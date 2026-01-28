"""
WebSocket 서버 모듈
- asyncio + websockets 라이브러리 사용
- Admin UI 연결 관리
- JSON 메시지 수신/응답
"""

import asyncio
import json
from typing import Set, Optional

import websockets
from websockets.server import WebSocketServerProtocol

from .config import Config
from .request_handler import RequestHandler


class WebSocketHandler:
    """WebSocket 서버"""

    def __init__(self, config: Config, request_handler: RequestHandler):
        self.config = config
        self.request_handler = request_handler
        self.clients: Set[WebSocketServerProtocol] = set()
        self.server: Optional[websockets.WebSocketServer] = None

    async def handle_client(self, websocket: WebSocketServerProtocol):
        """클라이언트 연결 처리"""
        self.clients.add(websocket)
        client_info = f"{websocket.remote_address}"
        print(f"[WebSocket] Client connected: {client_info}")

        try:
            async for message in websocket:
                print(f"[WebSocket] Received from {client_info}: {message}")

                # 요청 처리
                response = self.request_handler.handle_message(message)

                # 응답 전송
                response_json = json.dumps(response, ensure_ascii=False)
                await websocket.send(response_json)
                print(f"[WebSocket] Sent to {client_info}: {response_json}")

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[WebSocket] Client disconnected: {client_info} ({e})")
        except Exception as e:
            print(f"[WebSocket] Error with {client_info}: {e}")
        finally:
            self.clients.discard(websocket)
            print(f"[WebSocket] Client removed: {client_info}")

    async def broadcast(self, message: dict):
        """모든 클라이언트에 메시지 브로드캐스트"""
        if not self.clients:
            return

        message_json = json.dumps(message, ensure_ascii=False)
        await asyncio.gather(
            *[client.send(message_json) for client in self.clients],
            return_exceptions=True
        )

    async def start(self):
        """WebSocket 서버 시작"""
        self.server = await websockets.serve(
            self.handle_client,
            self.config.websocket_host,
            self.config.websocket_port
        )
        print(f"[WebSocket] Server started on ws://{self.config.websocket_host}:{self.config.websocket_port}")
        return self.server

    async def stop(self):
        """WebSocket 서버 정지"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print("[WebSocket] Server stopped")

    def get_client_count(self) -> int:
        """연결된 클라이언트 수"""
        return len(self.clients)
