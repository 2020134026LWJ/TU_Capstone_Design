"""AGV 메인 컨트롤러 - 플랫폼 독립 (실물/시뮬 동일)"""

import time

from hardware.base import HardwarePack
from aruco_detector import ArucoDetector
from mqtt_handler import MQTTHandler, TOPIC_PLAN, TOPIC_SHELF_CMD, TOPIC_CONTROL
from navigation import Navigator

# ─── 설정 ───
MARKER_DETECT_INTERVAL = 5
STATE_PUBLISH_INTERVAL = 6
MQTT_HOST = "localhost"
MQTT_PORT = 1883


class AGVMainController:
    """플랫폼 독립 AGV 컨트롤러"""

    def __init__(self, hw: HardwarePack):
        self.hw = hw

        # ─── 공통 모듈 초기화 ───
        self.aruco = ArucoDetector(hw.rid)
        self.mqtt = MQTTHandler(hw.rid, MQTT_HOST, MQTT_PORT)
        self.nav = Navigator(
            position=hw.position,
            motors=hw.motors,
            rid=hw.rid,
            start_node=hw.start_node,
        )

        # ─── 선반 상태 ───
        self.carrying_shelf = None

        # ─── MQTT 스레드 → 메인 루프 핸드오프 (race condition 방지) ───
        self._pending_plan = None    # (node_path, goal) | None
        self._pending_resume = False

        # ─── 콜백 연결 ───
        self.nav.set_on_arrived(self._on_goal_arrived)
        self.nav.set_on_node(self._on_intermediate_node)
        self.mqtt.register_callback(TOPIC_PLAN, self._handle_plan)
        self.mqtt.register_callback(TOPIC_SHELF_CMD, self._handle_shelf_cmd)
        self.mqtt.register_callback(TOPIC_CONTROL, self._handle_control)
        self.mqtt.connect()

    # ─── MQTT 콜백 ───

    def _handle_plan(self, data):
        """경로 계획 수신 → 메인 루프에서 처리 (race condition 방지)"""
        for r in data.get("robots", []):
            if int(r.get("rid", -1)) != self.hw.rid:
                continue
            node_path = r.get("node_path", [])
            goal = r.get("goal")
            if node_path and len(node_path) > 1:
                self._pending_plan = (node_path, goal)
            elif goal is not None:
                self.nav.set_at_goal(goal)
                self.mqtt.publish_arrived(goal)
            break

    def _handle_shelf_cmd(self, data):
        """선반 리프트 명령 처리"""
        if int(data.get("rid", -1)) != self.hw.rid:
            return

        command = data.get("command")
        shelf_id = data.get("shelf_id")
        print(f"[AGV {self.hw.rid}] Shelf cmd: {command} shelf {shelf_id}")

        if command == "pickup":
            self.hw.lift.lift_up()
            self.carrying_shelf = shelf_id
            self.mqtt.publish_shelf_ack(shelf_id, "pickup")
        elif command == "putdown":
            self.hw.lift.lift_down()
            if self.carrying_shelf:
                x, y = self.hw.position.get_position()
                self.hw.shelf_supervisor.detach_shelf(self.carrying_shelf, x, y)
            self.carrying_shelf = None
            self.mqtt.publish_shelf_ack(shelf_id, "putdown")

    # ─── 도착 콜백 ───

    def _on_goal_arrived(self, node: int):
        """Navigator가 목표 도착 시 호출"""
        self.mqtt.publish_arrived(node)

    def _on_intermediate_node(self, node: int):
        """Navigator가 중간 노드 통과 시 호출"""
        self.mqtt.publish_position(node)

    def _handle_control(self, data):
        """서버 제어 명령 수신 (resume)"""
        if int(data.get("rid", -1)) != self.hw.rid:
            return
        if data.get("cmd") == "resume":
            self._pending_resume = True

    # ─── 상태 발행 ───

    def _publish_state(self):
        state = {
            "rid": self.hw.rid,
            "current_node": self.nav.current_node,
            "state": self.nav.state,
            "carrying_shelf": self.carrying_shelf,
            "path_remaining": len(self.nav.path_queue),
            "detected_marker": self.aruco.detected_marker,
            "ts": int(time.time()),
        }
        self.mqtt.publish_state(state)

    # ─── 메인 루프 ───

    def run(self):
        rid = self.hw.rid
        aruco_ok = self.hw.camera is not None and self.aruco.available
        print(f"[AGV {rid}] Started at node {self.hw.start_node}")
        print(f"[AGV {rid}] ArUco: {'enabled' if aruco_ok else 'disabled'}")

        # 웜업
        for _ in range(10):
            self.hw.timestep.step()

        state_counter = 0
        marker_counter = 0

        while self.hw.timestep.step():
            # ─── pending plan 처리 ───
            if self._pending_plan is not None:
                node_path, goal = self._pending_plan
                self._pending_plan = None
                start_node = node_path[0]

                if self.nav.state == "IDLE":
                    # IDLE: 정상 출발
                    self.nav.set_plan(list(node_path[1:]), goal)
                    print(f"[AGV {rid}] Plan applied (IDLE): {node_path} → goal {goal}")
                elif self.nav.state == "NODE_WAIT" and self.nav.current_node == start_node:
                    # start_node에서 대기 중: path 갱신 후 arrived 재발행 → 서버 resume 트리거
                    self.nav.path_queue = list(node_path[1:])
                    self.nav.goal_node = goal
                    self.mqtt.publish_arrived(start_node)
                    print(f"[AGV {rid}] Plan applied (NODE_WAIT@{start_node}): re-arrived → {goal}")
                else:
                    # MOVING 또는 다른 노드 NODE_WAIT: path만 교체, 스냅 금지
                    self.nav.path_queue = list(node_path[1:])
                    self.nav.goal_node = goal
                    print(f"[AGV {rid}] Plan applied (MOVING): path replaced → {goal}")

            # ─── pending resume 처리 ───
            if self._pending_resume:
                self._pending_resume = False
                self.nav.resume()
                print(f"[AGV {rid}] ← resume")

            self.nav.update()

            # 선반 운반 중이면 위치 동기화
            if self.carrying_shelf is not None:
                x, y = self.hw.position.get_position()
                self.hw.shelf_supervisor.move_shelf_to_robot(self.carrying_shelf, x, y)

            # ArUco 마커 인식 (주기적)
            marker_counter += 1
            if marker_counter >= MARKER_DETECT_INTERVAL:
                if self.hw.camera:
                    gray = self.hw.camera.capture()
                    prev_marker = self.aruco.detected_marker
                    marker_id = self.aruco.detect(gray)
                    if marker_id is not None and marker_id != prev_marker:
                        self.mqtt.publish_marker(marker_id)
                marker_counter = 0

            # 상태 발행
            state_counter += 1
            if state_counter >= STATE_PUBLISH_INTERVAL:
                self._publish_state()
                state_counter = 0

        self.mqtt.disconnect()
