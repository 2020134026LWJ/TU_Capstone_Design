"""
AGV 서버 메인 진입점
TU Capstone Design - AGV 물류 피킹 시스템

모든 모듈 초기화 및 이벤트 루프 실행

실행 방법:
    python -m server.main
"""

import asyncio
import signal
import sys

from .config import Config
from .path_planner import PathPlanner
from .mqtt_publisher import MQTTPublisher
from .robot_manager import RobotManager
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
        self.request_handler = RequestHandler(
            config=self.config,
            path_planner=self.path_planner,
            mqtt_publisher=self.mqtt_publisher,
            robot_manager=self.robot_manager
        )
        self.websocket_handler = WebSocketHandler(
            config=self.config,
            request_handler=self.request_handler
        )

        print("[AGVServer] Modules initialized")

    async def start(self):
        """서버 시작"""
        print("[AGVServer] Starting server...")

        # MQTT 연결
        if not self.mqtt_publisher.connect():
            print("[AGVServer] Warning: MQTT connection failed")

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
