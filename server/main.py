"""
AGV 서버 메인 진입점 — TU Capstone Design AGV 물류 피킹 시스템

모든 모듈 초기화 + MQTT/WebSocket 이벤트 루프 실행.
실행: python -m server.main

────────────────────────────────────────────────────────────────────────
이벤트 → 핸들러 → 다이어그램(FLOWCHART.md) 매핑
────────────────────────────────────────────────────────────────────────
  MQTT 토픽                  type            핸들러 (mixin)                  다이어그램
  ──────────────────────────────────────────────────────────────────────
  /agv/marker             marker_report   MarkerMixin._handle_marker_report   위치갱신/STG/TRG/U
  /agv/cmd_ack            cmd_ack         MarkerMixin._handle_cmd_ack         turn/lift 완료
  warehouse/order/start   start_order     WorkflowMixin._handle_start_order   주문 진입
  warehouse/shelf/complete shelf_complete WorkflowMixin._handle_shelf_complete Point C(퇴출)
  warehouse/order/complete order_complete WorkflowMixin._handle_order_complete 주문 종료

  서버 → 외부 발행: /agv/cmd (AGV 명령) / warehouse/shelf/arrived (GUI 도착알림)

다이어그램 노드 ↔ 코드:
  STG    회랑 점유 확인→진입/대기   staging_manager.should_stage
  TRG    트리거 통과→대기AGV 해제   staging_manager.handle_marker_trigger
  Node U 복귀중 동일선반 신규주문    WorkflowMixin._try_intercept_returning_shelf
  Point C shelf_complete→퇴출 시작   WorkflowMixin._handle_shelf_complete
  F-node 선반 가용성 6분기           WorkflowMixin._get_shelf_availability

────────────────────────────────────────────────────────────────────────
재료 (RequestHandler 상태 변수 — mixin들이 self로 공유)
────────────────────────────────────────────────────────────────────────
  reservation             ReservationService  미래 점유 단일 진실 (시공간 예약). 충돌/교착 예방의 핵심
  command_queues          Dict[rid,CmdQueue]  AGV별 cmd lifecycle (in_flight/예약/blocked 추론)
  staging_manager         StagingManager      회랑 점유(=reservation 파생) + 대기 큐 + 트리거
  robot/shelf/task_manager                    도메인 상태 (로봇 6단계 / 선반 3상태 / 태스크)
  _staged_to_ws           Dict[rid,tuple]     회랑 조기해제 후 staging 미도착 로봇
  _forwarded_shelf_handlers Dict[shelf,rid]   포워딩 선반 재픽업 담당 로봇

────────────────────────────────────────────────────────────────────────
대표 흐름 — start_order 따라가기
────────────────────────────────────────────────────────────────────────
  1. (GUI/mqtt_test) warehouse/order/start {사용자ID, 주문번호} 발행
  2. _handle_mqtt_gui → RequestHandler.handle_message → _handle_start_order
  3. OrderOptimizer: xlsx에서 주문 로드 + NN으로 선반 방문순서 최적화
  4. TaskManager: 선반당 태스크 1개 생성 (task_id = T{user}_{order}_{idx})
  5. _try_assign_pending_tasks → get_available_robot → _plan_and_publish_move
  6. _plan_and_publish_move: should_stage(STG) → A*(reservation 기반) → cmd 큐 → /agv/cmd
  7. AGV가 마커 보고 → _handle_marker_report → 위치갱신 + 다음 cmd / 도착(_process_arrival)
  8. WS 도착: 선반 AT_WORKSTATION + 로봇 WAITING_FOR_PICK → warehouse/shelf/arrived (GUI 셀 활성)
  9. shelf_complete(Point C) → RETURN_SHELF / FORWARD_SHELF 분기 → 퇴출
 10. 홈 노드 복귀 → IDLE → 다음 태스크
"""

import asyncio
import json
import signal
import sys

from .config import Config
from .path_planner import PathPlanner
from .mqtt_client import MQTTClient
from .robot_manager import RobotManager
from .shelf_manager import ShelfManager
from .staging_manager import StagingManager
from .task_manager import TaskManager
from .request_handler import RequestHandler
from .websocket_handler import WebSocketHandler


class AGVServer:
    """AGV 서버"""

    def __init__(self, config: Config = None):
        self.config = config or Config()
        self.running = False

        # 모듈 초기화
        print("[AGVServer] Initializing modules...")

        self.path_planner = PathPlanner(self.config.map_file)
        self.mqtt_publisher = MQTTClient(self.config)
        self.robot_manager = RobotManager(self.config)
        self.shelf_manager = ShelfManager(self.config.shelf_config_file)
        self.staging_manager = StagingManager(
            self.shelf_manager.workstations,
            get_robot_node=lambda rid: (
                self.robot_manager.get_robot(rid).current_node
                if self.robot_manager.get_robot(rid) else None
            ),
            get_robot_planned_path=lambda rid: (
                self.robot_manager.get_robot(rid).planned_path
                if self.robot_manager.get_robot(rid) else None
            ),
        )
        self.task_manager = TaskManager(self.shelf_manager, self.path_planner)
        self.request_handler = RequestHandler(
            config=self.config,
            path_planner=self.path_planner,
            mqtt_publisher=self.mqtt_publisher,
            robot_manager=self.robot_manager,
            shelf_manager=self.shelf_manager,
            staging_manager=self.staging_manager,
            task_manager=self.task_manager,
        )
        self.websocket_handler = WebSocketHandler(
            config=self.config,
            request_handler=self.request_handler,
        )

        # 브로드캐스트 콜백 연결
        self.request_handler.set_broadcast_callback(self.websocket_handler.broadcast)

        print("[AGVServer] Modules initialized")

    async def start(self):
        """서버 시작"""
        print("[AGVServer] Starting server...")

        # MQTT 연결 + 구독
        if not self.mqtt_publisher.connect():
            print("[AGVServer] Warning: MQTT connection failed")
        else:
            self._setup_mqtt_subscriptions()

        # WebSocket 서버 시작
        await self.websocket_handler.start()

        self.running = True
        print("[AGVServer] Server is running")
        print(f"[AGVServer] WebSocket: ws://{self.config.websocket_host}:{self.config.websocket_port}")
        print(f"[AGVServer] MQTT: {self.config.mqtt_host}:{self.config.mqtt_port}")
        print("[AGVServer] Press Ctrl+C to stop")

        # 서버 유지
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    def _setup_mqtt_subscriptions(self):
        """AGV 컨트롤러 및 GUI로부터 MQTT 메시지 수신 설정"""
        self.mqtt_publisher.subscribe(
            self.config.mqtt_topic_marker,
            lambda data: self._handle_mqtt_marker(data),
        )
        self.mqtt_publisher.subscribe(
            self.config.mqtt_topic_cmd_ack,
            lambda data: self._handle_mqtt_cmd_ack(data),
        )
        # GUI/테스트 도구가 주문 시작 시 발행. AGV는 stock 검증 안 함(validate_stock 제거됨).
        self.mqtt_publisher.subscribe(
            "warehouse/order/start",
            lambda data: self._handle_mqtt_gui({
                "type": "start_order",
                "사용자ID": data.get("사용자ID"),
                "주문번호": data.get("주문번호"),
            }),
        )
        self.mqtt_publisher.subscribe(
            "warehouse/shelf/complete",
            lambda data: self._handle_mqtt_gui({
                "type": "shelf_complete",
                "사용자ID": data.get("사용자ID"),
            }),
        )
        self.mqtt_publisher.subscribe(
            "warehouse/order/complete",
            lambda data: self._handle_mqtt_gui({
                "type": "order_complete",
                "사용자ID": data.get("사용자ID"),
                "주문번호": data.get("주문번호"),
            }),
        )
        print("[AGVServer] MQTT subscriptions ready "
              "(/agv/marker, /agv/cmd_ack, "
              "warehouse/order/start, warehouse/shelf/complete, warehouse/order/complete)")

    def _handle_mqtt_marker(self, data):
        """AGV 마커 인식 → 위치 보고 + 다음 명령 결정"""
        data["type"] = "marker_report"
        result = self.request_handler.handle_message(json.dumps(data))
        rid = data.get("rid")
        marker_id = data.get("marker_id")
        print(f"[AGVServer] Marker: AGV-{rid} at node {marker_id} → {result.get('action', '?')}")

    def _handle_mqtt_cmd_ack(self, data):
        """AGV 명령 완료 보고 → request_handler 라우팅"""
        data["type"] = "cmd_ack"
        result = self.request_handler.handle_message(json.dumps(data))
        print(f"[AGVServer] cmd_ack: AGV-{data.get('rid')} {data.get('cmd')} → {result.get('action', '?')}")

    def _handle_mqtt_gui(self, data):
        """GUI로부터 MQTT 메시지 수신 → request_handler 라우팅"""
        result = self.request_handler.handle_message(json.dumps(data))
        msg_type = data.get("type", "?")
        status = result.get("action", result.get("success", "?"))
        print(f"[AGVServer] GUI MQTT ({msg_type}) → {status}")

    async def stop(self):
        """서버 정지"""
        print("\n[AGVServer] Stopping server...")
        self.running = False

        await self.websocket_handler.stop()
        self.mqtt_publisher.disconnect()

        # REFACTOR F Phase 1 — 사후 대응 7종 baseline 카운터 출력
        counters = dict(self.request_handler._refactor_f_counters)
        counters['staging_cascade'] = self.request_handler.staging_manager._cascade_count
        print("\n[REFACTOR F Phase 1] Reactive-pattern trigger counts:")
        for key, value in counters.items():
            print(f"  {key:24s} : {value}")
        print()

        print("[AGVServer] Server stopped")

    def handle_signal(self, signum, frame):
        """시그널 핸들러"""
        self.running = False


async def main():
    """메인 함수"""
    print("=" * 50)
    print("AGV Server for Webots Simulation")
    print("TU Capstone Design - AGV 물류 피킹 시스템")
    print("=" * 50)

    config = Config.from_env()
    server = AGVServer(config)

    # 시그널 핸들러 설정
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    try:
        await server.start()
    except KeyboardInterrupt:
        await server.stop()


def run():
    """실행 함수"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[AGVServer] Interrupted")
        sys.exit(0)


if __name__ == "__main__":
    run()
