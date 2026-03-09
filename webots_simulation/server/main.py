"""
AGV 서버 메인 진입점
TU Capstone Design - AGV 물류 피킹 시스템

모든 모듈 초기화 및 이벤트 루프 실행

실행 방법:
    python -m server.main
"""

import asyncio
import json
import signal
import sys

from .config import Config
from .path_planner import PathPlanner
from .mqtt_publisher import MQTTPublisher
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
        self.mqtt_publisher = MQTTPublisher(self.config)
        self.robot_manager = RobotManager(self.config)
        self.shelf_manager = ShelfManager(self.config.shelf_config_file)
        self.staging_manager = StagingManager(self.shelf_manager.workstations)
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
            "/agv/arrived",
            lambda data: self._handle_mqtt_arrived(data),
        )
        self.mqtt_publisher.subscribe(
            self.config.mqtt_topic_shelf_ack,
            lambda data: self._handle_mqtt_shelf_ack(data),
        )
        self.mqtt_publisher.subscribe(
            "/agv/marker",
            lambda data: self._handle_mqtt_marker(data),
        )
        # [추가] warehouse GUI(라즈베리파이)로부터 주문/선반완료/주문완료 수신
        # 기존 agv/algorithm 토픽 대신 warehouse/* 토픽 3개로 분리
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
              "(/agv/arrived, /agv/shelf_ack, /agv/marker, "
              "warehouse/order/start, warehouse/shelf/complete, warehouse/order/complete)")

    def _handle_mqtt_arrived(self, data):
        """AGV 도착/위치 이벤트 → request_handler 라우팅"""
        if data.get("type") == "robot_position":
            self.request_handler.handle_message(json.dumps(data))
            return
        data["type"] = "robot_arrived"
        result = self.request_handler.handle_message(json.dumps(data))
        print(f"[AGVServer] MQTT arrived: robot {data.get('rid')} at node {data.get('node')} → {result.get('action', '?')}")

    def _handle_mqtt_shelf_ack(self, data):
        """AGV 선반 리프트 완료 이벤트 → request_handler 라우팅"""
        data["type"] = "shelf_ack"
        result = self.request_handler.handle_message(json.dumps(data))
        print(f"[AGVServer] MQTT shelf_ack: robot {data.get('rid')} {data.get('command')} shelf {data.get('shelf_id')} → {result.get('action', '?')}")

    def _handle_mqtt_marker(self, data):
        """AGV 마커 인식 이벤트 → 스테이징 트리거 처리"""
        rid = data.get("rid")
        marker_id = data.get("marker_id")
        if rid is None or marker_id is None:
            return

        released = self.request_handler.handle_marker_trigger(rid, marker_id)
        if released:
            print(f"[AGVServer] Marker trigger: AGV-{rid} at marker {marker_id} → "
                  f"releasing AGV-{released.rid} to W{released.target_ws}")

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
