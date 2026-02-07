"""
WebSocket 테스트 클라이언트 - UI 역할
서버와 WebSocket 통신으로 주문 시작, 물품 픽업 완료 등 시뮬레이션

사용법:
    python websocket_test.py
"""

import asyncio
import json
import websockets


WS_URL = "ws://localhost:8765"


class WebSocketTestClient:
    """WebSocket 테스트 클라이언트"""

    def __init__(self):
        self.websocket = None
        self.connected = False

    async def connect(self):
        """서버에 연결"""
        try:
            self.websocket = await websockets.connect(WS_URL)
            self.connected = True
            print(f"[Client] Connected to {WS_URL}")
            return True
        except Exception as e:
            print(f"[Client] Connection failed: {e}")
            return False

    async def disconnect(self):
        """연결 종료"""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            print("[Client] Disconnected")

    async def send_and_receive(self, message: dict) -> dict:
        """메시지 전송 및 응답 수신"""
        if not self.connected:
            print("[Client] Not connected!")
            return {}

        msg_json = json.dumps(message, ensure_ascii=False)
        print(f"\n[Client] Sending: {msg_json}")

        await self.websocket.send(msg_json)
        response = await self.websocket.recv()

        result = json.loads(response)
        print(f"[Client] Received: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return result

    async def start_order(self, user_id: int, order_id: int):
        """주문 시작"""
        return await self.send_and_receive({
            "type": "start_order",
            "사용자ID": user_id,
            "주문번호": order_id,
        })

    async def pick_complete(self, task_id: str, item: str):
        """물품 픽업 완료"""
        return await self.send_and_receive({
            "type": "pick_complete",
            "task_id": task_id,
            "item": item,
        })

    async def robot_arrived(self, rid: int, node: int):
        """로봇 도착 (bridge 역할 테스트)"""
        return await self.send_and_receive({
            "type": "robot_arrived",
            "rid": rid,
            "node": node,
        })

    async def get_status(self):
        """시스템 상태 조회"""
        return await self.send_and_receive({
            "type": "status_request",
        })

    async def get_task_status(self, task_id: str = None):
        """작업 상태 조회"""
        msg = {"type": "task_status_request"}
        if task_id:
            msg["task_id"] = task_id
        return await self.send_and_receive(msg)

    async def get_shelf_status(self):
        """선반 상태 조회"""
        return await self.send_and_receive({
            "type": "shelf_status_request",
        })


def print_menu():
    """메뉴 출력"""
    print("\n" + "=" * 50)
    print("  WebSocket Test Client - UI Simulator")
    print("=" * 50)
    print("  [1] 사용자1 주문 시작")
    print("  [2] 사용자2 주문 시작")
    print("  [3] 물품 픽업 완료")
    print("  [4] 로봇 도착 시뮬레이션")
    print("  [5] 시스템 상태 조회")
    print("  [6] 작업 상태 조회")
    print("  [7] 선반 상태 조회")
    print("  [0] 종료")
    print("-" * 50)


async def main():
    client = WebSocketTestClient()

    if not await client.connect():
        print("서버에 연결할 수 없습니다. 서버가 실행 중인지 확인하세요.")
        print("  python -m server.main")
        return

    try:
        while True:
            print_menu()
            choice = input("선택: ").strip()

            if choice == "0":
                break

            elif choice == "1":
                # 사용자1 주문
                orders = [1, 2]  # 사용자1의 주문 목록
                print(f"  사용자1 주문 목록: {orders}")
                order_id = input("  주문번호 입력: ").strip()
                if order_id.isdigit():
                    await client.start_order(1, int(order_id))

            elif choice == "2":
                # 사용자2 주문
                orders = [1, 2, 3]
                print(f"  사용자2 주문 목록: {orders}")
                order_id = input("  주문번호 입력: ").strip()
                if order_id.isdigit():
                    await client.start_order(2, int(order_id))

            elif choice == "3":
                # 물품 픽업 완료
                task_id = input("  작업 ID (예: ORDER_1_1): ").strip()
                item = input("  물품명 (예: 드롭스): ").strip()
                if task_id and item:
                    await client.pick_complete(task_id, item)

            elif choice == "4":
                # 로봇 도착
                rid = input("  로봇 ID (1 or 2): ").strip()
                node = input("  도착 노드: ").strip()
                if rid.isdigit() and node.isdigit():
                    await client.robot_arrived(int(rid), int(node))

            elif choice == "5":
                await client.get_status()

            elif choice == "6":
                task_id = input("  작업 ID (빈칸=전체): ").strip()
                await client.get_task_status(task_id if task_id else None)

            elif choice == "7":
                await client.get_shelf_status()

            else:
                print("  잘못된 선택입니다.")

    except KeyboardInterrupt:
        print("\n\n[Client] Interrupted")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
