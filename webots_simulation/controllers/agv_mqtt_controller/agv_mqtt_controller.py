"""
AGV MQTT Controller for Webots Simulation
TU Capstone Design - AGV Logistics Picking System

Differential Drive AGV Controller with KIVA-style Lift
- Grid-based 90-degree movement
- MQTT communication with bridge.py
- Supervisor API for shelf manipulation
- Lift motor for picking up / putting down shelves
"""

import json
import math
import sys
import time

# 컨트롤러 디렉토리를 path에 추가 (로컬 paho 패키지 사용)
import os
controller_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, controller_dir)

from controller import Supervisor

# MQTT 라이브러리
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
    print("[AGV] paho-mqtt loaded successfully")
except ImportError as e:
    print(f"[AGV] WARNING: paho-mqtt not installed. Error: {e}")
    MQTT_AVAILABLE = False


# ============================================================
# 설정
# ============================================================
MQTT_HOST = "localhost"
MQTT_PORT = 1883
TOPIC_LOWCMD = "/agv/lowcmd"
TOPIC_PLAN = "/agv/plan"
TOPIC_STATE = "/agv/state"
TOPIC_ARRIVED = "/agv/arrived"
TOPIC_SHELF_CMD = "/agv/shelf_cmd"
TOPIC_SHELF_ACK = "/agv/shelf_ack"

CELL_SIZE = 1.0  # 그리드 한 칸 크기 (미터)
GRID_COLS = 7
GRID_ROWS = 7

# 이동 파라미터
MAX_SPEED = 6.28  # 최대 휠 속도 (rad/s)
MOVE_SPEED = 3.0  # 이동 시 휠 속도
TURN_SPEED = 2.0  # 회전 시 휠 속도

POSITION_TOLERANCE = 0.05  # 위치 도달 허용 오차 (m)
ANGLE_TOLERANCE = 0.03  # 각도 도달 허용 오차 (rad, 약 1.7도)

# 방향 정의 (라디안)
DIR_EAST = 0.0           # +X 방향 (오른쪽)
DIR_NORTH = math.pi/2    # +Y 방향 (위)
DIR_WEST = math.pi       # -X 방향 (왼쪽)
DIR_SOUTH = -math.pi/2   # -Y 방향 (아래)

# 리프트 파라미터
LIFT_UP = 0.10       # 리프트 최대 높이 (m)
LIFT_DOWN = 0.0      # 리프트 최소 높이 (m)
LIFT_TOLERANCE = 0.005  # 리프트 위치 허용 오차 (m)
SHELF_CARRY_Z = 0.06    # 선반이 AGV 위에 올라간 높이 오프셋


class AGVController:
    """디퍼렌셜 드라이브 AGV 컨트롤러 (Supervisor + Lift)"""

    def __init__(self):
        self.robot = Supervisor()
        self.timestep = int(self.robot.getBasicTimeStep())

        # 컨트롤러 인자: [rid, start_node]
        self.rid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
        self.start_node = int(sys.argv[2]) if len(sys.argv) > 2 else 1

        print(f"[AGV {self.rid}] Init - start node: {self.start_node}")

        # 센서 초기화
        self.gps = self.robot.getDevice("gps")
        self.gps.enable(self.timestep)

        self.compass = self.robot.getDevice("compass")
        self.compass.enable(self.timestep)

        # 디퍼렌셜 드라이브 휠 모터 (Pioneer 3-DX 기준)
        self.left_motor = self.robot.getDevice("left wheel")
        self.right_motor = self.robot.getDevice("right wheel")

        self.left_motor.setPosition(float('inf'))
        self.right_motor.setPosition(float('inf'))
        self.left_motor.setVelocity(0.0)
        self.right_motor.setVelocity(0.0)

        # 리프트 모터/센서 초기화
        self.lift_motor = self.robot.getDevice("lift_motor")
        self.lift_sensor = self.robot.getDevice("lift_sensor")
        self.lift_sensor.enable(self.timestep)
        self.lift_motor.setPosition(LIFT_DOWN)

        # 상태 변수
        self.current_node = self.start_node
        self.target_node = self.start_node
        self.speed = 0.0
        self.progress = 0.0

        # 이동 상태 머신: IDLE, TURNING, MOVING, LIFTING, LOWERING
        self.state = "IDLE"
        self.target_angle = 0.0
        self.target_pos = self.node_to_world(self.start_node)
        self.move_start_pos = self.target_pos

        # 경로 큐 (서버에서 /agv/plan으로 받은 전체 경로를 노드 단위로 분해)
        self.path_queue = []       # [node1, node2, ...] 이동할 노드 큐
        self.final_goal = None     # 최종 목적지 노드

        # 선반 운반 상태 (Supervisor API)
        self.carrying_shelf_id = None        # 운반 중인 선반 노드 ID (예: 9)
        self.carrying_shelf_node = None      # Supervisor Node 객체
        self.carrying_shelf_field = None     # translation 필드 참조

        # 대기 중인 shelf_cmd
        self.pending_shelf_cmd = None  # {"command": "pickup"/"putdown", "shelf_id": 9}

        # MQTT
        self.mqtt_client = None
        self.mqtt_connected = False
        if MQTT_AVAILABLE:
            self._setup_mqtt()

    def node_to_world(self, node_id):
        """Node ID → 월드 좌표"""
        # 작업대 노드 특수 처리
        if node_id == 50:  # W1
            return -0.5, 0.5
        elif node_id == 51:  # W2
            return -0.5, 6.5

        # 일반 그리드 노드 (1-49)
        idx = node_id - 1
        col = idx % GRID_COLS
        row = idx // GRID_COLS
        world_x = (col + 0.5) * CELL_SIZE
        world_y = (row + 0.5) * CELL_SIZE
        return world_x, world_y

    def world_to_node(self, x, y):
        """월드 좌표 → Node ID"""
        # 작업대 노드 특수 처리 (그리드 외부)
        if x < 0:
            if y < 3.5:
                return 50  # W1
            else:
                return 51  # W2

        col = int(x / CELL_SIZE)
        row = int(y / CELL_SIZE)
        col = max(0, min(GRID_COLS - 1, col))
        row = max(0, min(GRID_ROWS - 1, row))
        return row * GRID_COLS + col + 1

    def node_to_grid(self, node_id):
        """Node ID → 그리드 좌표 (col, row)"""
        idx = node_id - 1
        col = idx % GRID_COLS
        row = idx // GRID_COLS
        return col, row

    def grid_to_node(self, col, row):
        """그리드 좌표 → Node ID"""
        if 0 <= col < GRID_COLS and 0 <= row < GRID_ROWS:
            return row * GRID_COLS + col + 1
        return None

    def is_adjacent(self, node1, node2):
        """두 노드가 인접한지 확인 (상하좌우만)"""
        col1, row1 = self.node_to_grid(node1)
        col2, row2 = self.node_to_grid(node2)
        return abs(col1 - col2) + abs(row1 - row2) == 1

    def _setup_mqtt(self):
        """MQTT 클라이언트 설정"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message

        try:
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            print(f"[AGV {self.rid}] MQTT connecting...")
        except Exception as e:
            print(f"[AGV {self.rid}] MQTT connection failed: {e}")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        print(f"[AGV {self.rid}] MQTT connected, rc={rc}")
        client.subscribe(TOPIC_LOWCMD)
        client.subscribe(TOPIC_PLAN)
        client.subscribe(TOPIC_SHELF_CMD)
        print(f"[AGV {self.rid}] Subscribed: {TOPIC_LOWCMD}, {TOPIC_PLAN}, {TOPIC_SHELF_CMD}")
        self.mqtt_connected = True

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            return

        # /agv/plan 메시지 처리 (서버에서 전체 경로 수신)
        if msg.topic == TOPIC_PLAN:
            self._handle_plan_message(cmd)
            return

        # /agv/shelf_cmd 메시지 처리
        if msg.topic == TOPIC_SHELF_CMD:
            self._handle_shelf_cmd(cmd)
            return

        # /agv/lowcmd 메시지 처리 (기존 호환: 노드 단위 명령)
        if "rid" in cmd and int(cmd["rid"]) != self.rid:
            return

        new_target = cmd.get("target_node")
        v = float(cmd.get("v", 0.0))

        if new_target is not None:
            new_target = int(new_target)
            if new_target == self.target_node and self.state in ["TURNING", "MOVING"]:
                pass
            elif self.state == "IDLE" and new_target != self.current_node:
                self._set_new_target(new_target)

        self.speed = v

    def _handle_plan_message(self, plan):
        """
        /agv/plan 메시지에서 자신의 경로 추출 → 경로 큐에 저장

        서버 메시지 형식:
        {
            "robots": [
                {"rid": 1, "node_path": [50, 1, 2, 9], "speed": 0.3},
                {"rid": 2, "node_path": [51, 43, 37], "speed": 0.3}
            ],
            "speed": 0.3
        }
        """
        robots = plan.get("robots", [])
        speed = float(plan.get("speed", 0.3))

        for robot_info in robots:
            rid = int(robot_info.get("rid", -1))
            if rid != self.rid:
                continue

            node_path = robot_info.get("node_path", [])
            if len(node_path) < 2:
                print(f"[AGV {self.rid}] Plan received but path too short: {node_path}")
                return

            # 첫 노드는 현재 위치이므로 제외, 나머지를 큐에 저장
            self.path_queue = list(node_path[1:])
            self.final_goal = node_path[-1]
            self.speed = speed

            print(f"[AGV {self.rid}] Plan received: {node_path} (queue: {self.path_queue})")

            # 첫 번째 노드로 이동 시작
            self._process_next_in_queue()
            return

    def _handle_shelf_cmd(self, cmd):
        """
        /agv/shelf_cmd 메시지 처리

        메시지 형식:
        {"rid": 1, "command": "pickup", "shelf_id": 9}
        {"rid": 1, "command": "putdown", "shelf_id": 9}
        """
        rid = int(cmd.get("rid", -1))
        if rid != self.rid:
            return

        command = cmd.get("command")
        shelf_id = int(cmd.get("shelf_id", 0))

        print(f"[AGV {self.rid}] Shelf cmd received: {command} shelf {shelf_id}")

        # 대기 큐에 저장 (IDLE 상태에서 처리)
        self.pending_shelf_cmd = {"command": command, "shelf_id": shelf_id}

    def _process_next_in_queue(self):
        """경로 큐에서 다음 노드를 꺼내 이동 시작"""
        if not self.path_queue:
            return False

        next_node = self.path_queue.pop(0)
        print(f"[AGV {self.rid}] Queue -> node {next_node} (remaining: {len(self.path_queue)})")
        self._set_new_target(next_node)
        return True

    def _publish_arrived(self, node):
        """최종 목적지 도착 시 /agv/arrived 발행"""
        if not self.mqtt_connected:
            return

        payload = {
            "rid": self.rid,
            "node": node,
        }
        try:
            self.mqtt_client.publish(TOPIC_ARRIVED, json.dumps(payload), qos=1)
            print(f"[AGV {self.rid}] Published arrived: node {node}")
        except Exception as e:
            print(f"[AGV {self.rid}] Arrived publish failed: {e}")

    def _publish_shelf_ack(self, command, shelf_id):
        """리프트 동작 완료 후 shelf_ack 발행"""
        if not self.mqtt_connected:
            return

        payload = {
            "rid": self.rid,
            "command": command,
            "shelf_id": shelf_id,
        }
        try:
            self.mqtt_client.publish(TOPIC_SHELF_ACK, json.dumps(payload), qos=1)
            print(f"[AGV {self.rid}] Published shelf_ack: {command} shelf {shelf_id}")
        except Exception as e:
            print(f"[AGV {self.rid}] Shelf ack publish failed: {e}")

    def _set_new_target(self, new_target):
        """
        새 목표 Node 설정 - 인접 노드로만 이동
        서버에서 A* 경로 계획 후 bridge가 순차적으로 인접 노드만 전달함
        """
        self.target_node = new_target
        self.target_pos = self.node_to_world(new_target)
        self.progress = 0.0

        current_x, current_y = self.get_position()
        current_node = self.world_to_node(current_x, current_y)
        self.move_start_pos = (current_x, current_y)

        # 디버그: 현재 위치와 목표 위치 출력
        print(f"[AGV {self.rid}] DEBUG: current_pos=({current_x:.2f}, {current_y:.2f}), current_node={current_node}")
        print(f"[AGV {self.rid}] DEBUG: target_node={new_target}, target_pos={self.target_pos}")

        # 이미 목표 위치에 있음
        if current_node == new_target:
            self.current_node = new_target
            self.state = "IDLE"
            print(f"[AGV {self.rid}] Already at target node {new_target}")
            return

        # 작업대 노드에서 그리드로 이동할 때 특수 처리
        if self.current_node in [50, 51] or new_target in [50, 51]:
            print(f"[AGV {self.rid}] Workstation node involved, adjacency OK")
        elif not self.is_adjacent(current_node, new_target):
            print(f"[AGV {self.rid}] WARNING: target {new_target} is NOT adjacent to current {current_node}")

        # 이동 방향 계산
        dx = self.target_pos[0] - current_x
        dy = self.target_pos[1] - current_y

        print(f"[AGV {self.rid}] DEBUG: dx={dx:.2f}, dy={dy:.2f}")

        # 90도 단위로 목표 각도 결정
        if abs(dx) > abs(dy):
            # X 방향 이동
            self.target_angle = DIR_EAST if dx > 0 else DIR_WEST
        else:
            # Y 방향 이동
            self.target_angle = DIR_NORTH if dy > 0 else DIR_SOUTH

        self.state = "TURNING"
        print(f"[AGV {self.rid}] Moving to node {new_target}, angle: {math.degrees(self.target_angle):.0f}°")

    def get_position(self):
        """현재 GPS 위치"""
        values = self.gps.getValues()
        return values[0], values[1]

    def get_bearing(self):
        """현재 방향 (라디안, -π ~ π)"""
        values = self.compass.getValues()
        # Compass는 북쪽(+Y)을 기준으로 함
        angle = math.atan2(values[0], values[1])
        return angle

    def normalize_angle(self, angle):
        """각도를 -π ~ π 범위로 정규화"""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def set_motors(self, left, right):
        """모터 속도 설정"""
        self.left_motor.setVelocity(left)
        self.right_motor.setVelocity(right)

    def stop(self):
        """정지"""
        self.set_motors(0.0, 0.0)

    def _start_lift(self, shelf_id):
        """리프트 올리기 시작 (pickup)"""
        print(f"[AGV {self.rid}] Starting LIFT for shelf {shelf_id}")
        self.carrying_shelf_id = shelf_id

        # Supervisor API로 선반 노드 참조 획득
        def_name = f"SHELF_{shelf_id}"
        shelf_node = self.robot.getFromDef(def_name)
        if shelf_node:
            self.carrying_shelf_node = shelf_node
            self.carrying_shelf_field = shelf_node.getField("translation")
            print(f"[AGV {self.rid}] Supervisor: got shelf DEF={def_name}")
        else:
            print(f"[AGV {self.rid}] WARNING: Supervisor cannot find DEF={def_name}")

        # 리프트 모터 UP
        self.lift_motor.setPosition(LIFT_UP)
        self.state = "LIFTING"

    def _start_lower(self, shelf_id):
        """리프트 내리기 시작 (putdown)"""
        print(f"[AGV {self.rid}] Starting LOWER for shelf {shelf_id}")

        # 리프트 모터 DOWN
        self.lift_motor.setPosition(LIFT_DOWN)
        self.state = "LOWERING"

    def _update_shelf_position(self):
        """운반 중인 선반을 AGV 위치에 맞춰 이동 (매 timestep)"""
        if self.carrying_shelf_field is None:
            return

        agv_x, agv_y = self.get_position()
        self.carrying_shelf_field.setSFVec3f([agv_x, agv_y, SHELF_CARRY_Z])

    def update(self):
        """상태 머신 업데이트"""

        # 선반 운반 중이면 매 timestep 위치 업데이트
        if self.state == "MOVING" and self.carrying_shelf_node is not None:
            self._update_shelf_position()

        if self.state == "IDLE":
            self.stop()

            # IDLE 상태에서 pending_shelf_cmd 확인
            if self.pending_shelf_cmd is not None:
                cmd = self.pending_shelf_cmd
                self.pending_shelf_cmd = None
                if cmd["command"] == "pickup":
                    self._start_lift(cmd["shelf_id"])
                elif cmd["command"] == "putdown":
                    self._start_lower(cmd["shelf_id"])
                return

            if self.speed <= 0:
                return

        elif self.state == "LIFTING":
            self.stop()
            # 리프트 센서 확인
            lift_pos = self.lift_sensor.getValue()
            if abs(lift_pos - LIFT_UP) < LIFT_TOLERANCE:
                # 리프트 올리기 완료
                print(f"[AGV {self.rid}] LIFT complete (pos={lift_pos:.4f})")
                # 선반 최종 위치 한번 더 설정
                self._update_shelf_position()
                self._publish_shelf_ack("pickup", self.carrying_shelf_id)
                self.state = "IDLE"
            return

        elif self.state == "LOWERING":
            self.stop()
            # 리프트 센서 확인
            lift_pos = self.lift_sensor.getValue()
            if abs(lift_pos - LIFT_DOWN) < LIFT_TOLERANCE:
                # 리프트 내리기 완료 → 선반 최종 위치 고정 후 참조 해제
                print(f"[AGV {self.rid}] LOWER complete (pos={lift_pos:.4f})")
                if self.carrying_shelf_field is not None:
                    # 선반을 현재 AGV 위치의 바닥에 놓기
                    agv_x, agv_y = self.get_position()
                    self.carrying_shelf_field.setSFVec3f([agv_x, agv_y, 0.0])
                    print(f"[AGV {self.rid}] Shelf placed at ({agv_x:.2f}, {agv_y:.2f})")

                shelf_id = self.carrying_shelf_id
                self.carrying_shelf_node = None
                self.carrying_shelf_field = None
                self.carrying_shelf_id = None
                self._publish_shelf_ack("putdown", shelf_id)
                self.state = "IDLE"
            return

        current_x, current_y = self.get_position()
        current_angle = self.get_bearing()

        if self.state == "TURNING":
            # 목표 각도로 회전
            angle_diff = self.normalize_angle(self.target_angle - current_angle)

            if abs(angle_diff) < ANGLE_TOLERANCE:
                # 회전 완료 → 이동 Started
                self.stop()
                self.state = "MOVING"
                self.move_start_pos = (current_x, current_y)
                print(f"[AGV {self.rid}] Turn complete, moving forward")
            else:
                # 제자리 회전 (디퍼렌셜 드라이브)
                turn_speed = TURN_SPEED if angle_diff > 0 else -TURN_SPEED
                # 급격한 회전 방지
                turn_speed = turn_speed * min(1.0, abs(angle_diff) / 0.5)
                self.set_motors(-turn_speed, turn_speed)

        elif self.state == "MOVING":
            # 직진 이동
            target_x, target_y = self.target_pos
            dx = target_x - current_x
            dy = target_y - current_y
            distance = math.sqrt(dx*dx + dy*dy)

            # 진행률 계산
            start_x, start_y = self.move_start_pos
            total_dist = math.sqrt((target_x - start_x)**2 + (target_y - start_y)**2)
            if total_dist > 0.01:
                self.progress = max(0.0, min(1.0, 1.0 - distance / total_dist))
            else:
                self.progress = 1.0

            if distance < POSITION_TOLERANCE:
                # 목표 노드 도착
                self.stop()
                self.current_node = self.target_node
                self.progress = 1.0
                self.state = "IDLE"
                print(f"[AGV {self.rid}] Node {self.current_node} arrived")

                # 경로 큐에 다음 노드가 있으면 계속 이동
                if self.path_queue:
                    self._process_next_in_queue()
                elif self.final_goal is not None and self.current_node == self.final_goal:
                    # 최종 목적지 도착 → 서버에 알림
                    self._publish_arrived(self.current_node)
                    self.final_goal = None
            else:
                # 직진 중 방향 보정
                angle_to_target = math.atan2(dy, dx)
                angle_diff = self.normalize_angle(angle_to_target - current_angle)

                # 직진하면서 약간의 방향 보정
                correction = angle_diff * 2.0
                correction = max(-1.0, min(1.0, correction))

                left_speed = MOVE_SPEED - correction
                right_speed = MOVE_SPEED + correction

                self.set_motors(left_speed, right_speed)

    def publish_state(self):
        """MQTT로 상태 발행"""
        if not self.mqtt_connected:
            return

        state = {
            "rid": self.rid,
            "current_node": self.current_node,
            "target_node": self.target_node,
            "progress": round(self.progress, 2),
            "state": self.state,
            "carrying_shelf": self.carrying_shelf_id,
            "ts": int(time.time())
        }

        try:
            self.mqtt_client.publish(TOPIC_STATE, json.dumps(state), qos=0)
        except Exception as e:
            print(f"[AGV {self.rid}] State publish failed: {e}")

    def run(self):
        """메인 루프"""
        print(f"[AGV {self.rid}] Started (Supervisor mode)")

        # 초기 센서 읽기 대기
        for _ in range(10):
            self.robot.step(self.timestep)

        state_counter = 0

        while self.robot.step(self.timestep) != -1:
            self.update()

            # 상태 발행 (약 10Hz)
            state_counter += 1
            if state_counter >= 6:
                self.publish_state()
                state_counter = 0

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


if __name__ == "__main__":
    controller = AGVController()
    controller.run()
