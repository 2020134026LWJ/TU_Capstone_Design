"""
AGV 서버 메인 진입점 — TU Capstone Design AGV 물류 피킹 시스템

모든 모듈 초기화 + MQTT 이벤트 루프 실행.
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
  1. (GUI) warehouse/order/start {사용자ID, 주문번호, 작업대} 발행
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

import asyncio   # 비동기 이벤트 루프 (서버 유지 루프)
import json      # request_handler.handle_message는 JSON 문자열을 받으므로 dict→str 직렬화
import signal    # Ctrl+C(SIGINT)/종료(SIGTERM) → graceful shutdown
import sys       # 인터럽트 시 종료 코드 반환

# ─── 의존 모듈 (의존 방향: main → 각 매니저/플래너/통신, 한쪽) ───
from .config import Config
from .planning.path_planner import PathPlanner
from .comm.mqtt_client import MQTTClient
from .managers.robot import RobotManager, RobotStatus
from .managers.shelf import ShelfManager
from .managers.staging import StagingManager
from .managers.task import TaskManager
from .core.request_handler import RequestHandler


class AGVServer:
    """모든 모듈을 묶고 MQTT 이벤트 루프를 구동하는 최상위 컨테이너."""

    def __init__(self, config: Config = None):
        self.config = config or Config()   # 주입 없으면 기본 설정 사용
        self.running = False               # start()에서 True, stop()/시그널에서 False → 루프 종료

        # ─── 모듈 초기화 (의존 순서: 데이터 로더/플래너 → 매니저 → 핸들러) ───
        print("[AGVServer] Initializing modules...")

        self.path_planner = PathPlanner(self.config.map_file)      # map.json 로드 + A* 그래프 구성
        self.mqtt_publisher = MQTTClient(self.config)              # MQTT publish/subscribe 래퍼
        self.robot_manager = RobotManager(self.config)            # robot_config.json → 로봇 상태 머신
        self.shelf_manager = ShelfManager(self.config.shelf_config_file)  # 선반/작업대 상태

        # StagingManager는 로봇의 "현재 위치/계획경로"를 실시간 조회해야 함.
        # 순환 의존을 피하려고 직접 참조 대신 람다(콜백)로 robot_manager를 lazy 조회한다.
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
            # 수정 61 — 사람 픽킹 대기는 타임아웃 대상이 아니다 (사람 시간은 무한정)
            is_robot_waiting_for_pick=lambda rid: (
                self.robot_manager.get_robot(rid) is not None
                and self.robot_manager.get_robot(rid).status == RobotStatus.WAITING_FOR_PICK
            ),
        )
        self.task_manager = TaskManager(self.shelf_manager, self.path_planner)  # 주문→서브태스크 분해

        # RequestHandler = 두뇌. 위 매니저/플래너/통신을 모두 주입받아
        # 이벤트(마커/cmd_ack/주문)를 받아 결정하고 /agv/cmd를 발행한다 (3 mixin 다중상속).
        self.request_handler = RequestHandler(
            config=self.config,
            path_planner=self.path_planner,
            mqtt_publisher=self.mqtt_publisher,
            robot_manager=self.robot_manager,
            shelf_manager=self.shelf_manager,
            staging_manager=self.staging_manager,
            task_manager=self.task_manager,
        )
        print("[AGVServer] Modules initialized")

    async def start(self):
        """서버 시작 — MQTT 연결/구독 + 종료까지 유지 루프 진입."""
        print("[AGVServer] Starting server...")

        # MQTT 브로커 연결 성공 시에만 구독 설정
        if not self.mqtt_publisher.connect():
            print("[AGVServer] Warning: MQTT connection failed")
        else:
            self._setup_mqtt_subscriptions()

        self.running = True
        print("[AGVServer] Server is running")
        print(f"[AGVServer] MQTT: {self.config.mqtt_host}:{self.config.mqtt_port}")
        print("[AGVServer] Press Ctrl+C to stop")

        # 서버 유지 루프 — running=False(시그널/stop) 될 때까지 1초 단위로 살아있음.
        # 실제 일은 MQTT 콜백(별 스레드)이 이벤트 기반으로 처리.
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass   # 종료 시그널로 태스크가 취소되면 조용히 빠져나감

    def _setup_mqtt_subscriptions(self):
        """수신 토픽 → 핸들러 매핑 등록. (발행은 mqtt_publisher.publish_cmd로 별도)

        각 subscribe는 (토픽, 콜백). 콜백은 수신 dict를 받아 request_handler가
        이해하는 메시지로 가공 후 라우팅한다. AGV발(marker/cmd_ack)과 GUI발(주문)로 나뉨.
        """
        # AGV → 서버: 마커 인식 (위치 갱신 + 다음 명령 트리거)
        self.mqtt_publisher.subscribe(
            self.config.mqtt_topic_marker,
            lambda data: self._handle_mqtt_marker(data),
        )
        # AGV → 서버: turn/lift 명령 완료 보고
        self.mqtt_publisher.subscribe(
            self.config.mqtt_topic_cmd_ack,
            lambda data: self._handle_mqtt_cmd_ack(data),
        )
        # GUI/테스트 도구 → 서버: 주문 시작. AGV는 stock 검증 안 함(validate_stock 제거됨, 수정 50).
        self.mqtt_publisher.subscribe(
            "warehouse/order/start",
            lambda data: self._handle_mqtt_gui({
                "type": "start_order",
                "사용자ID": data.get("사용자ID"),
                "주문번호": data.get("주문번호"),
                "작업대": data.get("작업대"),
            }),
        )
        # GUI → 서버: 선반 피킹 완료(Point C) → AGV 퇴출(반납/포워딩) 시작
        self.mqtt_publisher.subscribe(
            "warehouse/shelf/complete",
            lambda data: self._handle_mqtt_gui({
                "type": "shelf_complete",
                "사용자ID": data.get("사용자ID"),
                "작업대": data.get("작업대"),
            }),
        )
        # GUI → 서버: 주문 전체 완료
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

    # ─── MQTT 콜백 어댑터 (수신 dict → handle_message용 메시지로 가공 + 로그) ───
    # 공통 패턴: type 태깅 → json.dumps → request_handler.handle_message → 결과 로그.

    def _handle_mqtt_marker(self, data):
        """AGV 마커 인식 → 위치 보고 + 다음 명령 결정 (MarkerMixin)."""
        data["type"] = "marker_report"   # 라우터가 분기할 메시지 타입 태깅
        result = self.request_handler.handle_message(json.dumps(data))
        rid = data.get("rid")
        marker_id = data.get("marker_id")
        # result.action 예: en_route / pending_replan_flushed / 도착 처리 결과
        print(f"[AGVServer] Marker: AGV-{rid} at node {marker_id} → {result.get('action', '?')}")

    def _handle_mqtt_cmd_ack(self, data):
        """AGV turn/lift 완료 보고 → request_handler 라우팅 (MarkerMixin._handle_cmd_ack)."""
        data["type"] = "cmd_ack"
        result = self.request_handler.handle_message(json.dumps(data))
        print(f"[AGVServer] cmd_ack: AGV-{data.get('rid')} {data.get('cmd')} → {result.get('action', '?')}")

    def _handle_mqtt_gui(self, data):
        """GUI발 메시지(start_order/shelf_complete/order_complete) → 라우팅 (WorkflowMixin)."""
        # 이미 _setup_mqtt_subscriptions에서 type을 붙여 넘기므로 그대로 라우팅
        result = self.request_handler.handle_message(json.dumps(data))
        msg_type = data.get("type", "?")
        status = result.get("action", result.get("success", "?"))
        print(f"[AGVServer] GUI MQTT ({msg_type}) → {status}")

    async def stop(self):
        """서버 정지 — MQTT 정리 + 진단 카운터 출력."""
        print("\n[AGVServer] Stopping server...")
        self.running = False           # 유지 루프 종료 신호

        self.mqtt_publisher.disconnect()

        # REFACTOR F Phase 1 — "사후 대응(reactive)" 패턴이 몇 번 발동했는지 baseline 측정.
        # 목표는 예방형 설계라 이 값들이 작을수록 좋음 (staging_cascade가 핵심 핫스팟 지표).
        counters = dict(self.request_handler._refactor_f_counters)
        counters['staging_cascade'] = self.request_handler.staging_manager._cascade_count
        print("\n[REFACTOR F Phase 1] Reactive-pattern trigger counts:")
        for key, value in counters.items():
            print(f"  {key:24s} : {value}")
        print()

        print("[AGVServer] Server stopped")

    def handle_signal(self, signum, frame):
        """동기 시그널 핸들러 (현재 미사용 — main()이 loop.add_signal_handler로 대체)."""
        self.running = False


async def main():
    """진입점 코루틴 — Config 로드 → 서버 생성 → 시그널 연결 → start()."""
    print("=" * 50)
    print("AGV Server for Webots Simulation")
    print("TU Capstone Design - AGV 물류 피킹 시스템")
    print("=" * 50)

    config = Config.from_env()        # 환경변수 오버라이드 허용 (host/port)
    server = AGVServer(config)

    # Ctrl+C / kill 시 graceful shutdown: 이벤트 루프에 시그널 → stop() 태스크 예약
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    try:
        await server.start()          # running=False 될 때까지 블록
    except KeyboardInterrupt:
        await server.stop()           # 시그널 핸들러 미동작 환경 폴백

    return None


def run():
    """동기 실행 래퍼 — `python -m server.main`의 실제 호출 대상."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[AGVServer] Interrupted")
        sys.exit(0)


# 모듈을 직접 실행할 때만 서버 기동 (import 시엔 실행 안 됨)
if __name__ == "__main__":
    run()
