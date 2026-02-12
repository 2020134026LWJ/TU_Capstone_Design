"""
WebSocket 테스트 클라이언트 - CLI 기반 UI 시뮬레이터

사용법:
    python websocket_test.py

흐름:
    > 시작 1           사용자1 주문 시작
    > 시작 2           사용자2 주문 시작
    (AGV가 각각 선반 가져오면)
    > 완료 1           사용자1 선반 수령 완료 → 다음 선반
    > 완료 2           사용자2 선반 수령 완료 → 다음 선반
"""

import asyncio
import json
import websockets

WS_URL = "ws://localhost:8765"


class UserOrder:
    """사용자별 주문 상태"""

    def __init__(self, user_id, task_id, shelves):
        self.user_id = user_id
        self.task_id = task_id
        self.shelves = shelves    # [{"order", "shelf_label", "shelf_node", "items"}, ...]
        self.current_shelf_idx = 0

    def is_done(self):
        return self.current_shelf_idx >= len(self.shelves)

    def current_shelf(self):
        if self.is_done():
            return None
        return self.shelves[self.current_shelf_idx]

    def advance(self):
        self.current_shelf_idx += 1


class CLIClient:

    def __init__(self):
        self.ws = None
        self.orders = {}  # user_id → UserOrder

    async def connect(self):
        try:
            self.ws = await websockets.connect(WS_URL)
            print(f"  서버 연결됨 ({WS_URL})")
            return True
        except Exception as e:
            print(f"  서버 연결 실패: {e}")
            print("  서버를 먼저 실행하세요:")
            print("  cd TU_Capstone_Design/webots_simulation && python3 -m server.main")
            return False

    async def send(self, msg):
        await self.ws.send(json.dumps(msg, ensure_ascii=False))
        response = await self.ws.recv()
        return json.loads(response)

    async def start_order(self, user_id):
        """주문 시작"""
        if user_id in self.orders and not self.orders[user_id].is_done():
            print(f"  사용자 {user_id}의 주문이 이미 진행 중입니다.")
            return

        result = await self.send({
            "type": "start_order",
            "사용자ID": user_id,
            "주문번호": 1,
        })

        if not result.get("success"):
            print(f"  주문 실패: {result.get('error')}")
            return

        task_id = result["task_id"]
        shelves = result.get("shelves", [])

        self.orders[user_id] = UserOrder(user_id, task_id, shelves)

        print(f"\n{'='*55}")
        print(f"  사용자 {user_id} 주문 시작 (AGV-{user_id})")
        print(f"{'='*55}")
        print(f"  선반 방문 순서:")
        for s in shelves:
            items_str = ", ".join(s["items"])
            print(f"    {s['order']}. 선반 {s['shelf_label']} → {items_str}")
        print(f"{'='*55}")

        self._show_current(user_id)

    def _show_current(self, user_id):
        """현재 선반 상태 표시"""
        order = self.orders.get(user_id)
        if not order or order.is_done():
            return

        shelf = order.current_shelf()
        idx = order.current_shelf_idx + 1
        total = len(order.shelves)
        items_str = ", ".join(shelf["items"])

        print(f"\n  [사용자{user_id}] [{idx}/{total}] AGV가 선반 {shelf['shelf_label']}을 가져오는 중...")
        print(f"           물품: {items_str}")

    async def shelf_done(self, user_id):
        """선반 완료 → 갖다놓고 다음 거"""
        order = self.orders.get(user_id)
        if not order:
            print(f"  사용자 {user_id}의 진행 중인 주문이 없습니다.")
            return

        if order.is_done():
            print(f"  사용자 {user_id}의 주문은 이미 완료됨")
            return

        shelf = order.current_shelf()
        items = shelf["items"]

        # 해당 선반의 모든 물품 pick_complete
        last_result = None
        for item in items:
            result = await self.send({
                "type": "pick_complete",
                "task_id": order.task_id,
                "item": item,
            })
            last_result = result

            if not result.get("success"):
                error = result.get("error", "")
                if "WAIT_PICKING" in error:
                    print(f"  [사용자{user_id}] AGV가 아직 선반을 가져오는 중입니다. 잠시 기다려주세요.")
                else:
                    print(f"  [사용자{user_id}] 오류: {error}")
                return

        items_str = ", ".join(items)
        next_action = last_result.get("next_action", "") if last_result else ""

        if next_action == "forward_shelf":
            forward_ws = last_result.get("forward_to_ws", "?")
            ws_label = "W1" if forward_ws == 33 else "W2" if forward_ws == 34 else f"WS{forward_ws}"
            print(f"  [사용자{user_id}] 선반 {shelf['shelf_label']} 완료 ({items_str})")
            print(f"           → 다른 작업대({ws_label})에서도 필요! 포워딩 중...")
        else:
            print(f"  [사용자{user_id}] 선반 {shelf['shelf_label']} 완료 ({items_str}) → 선반 복귀 중...")

        order.advance()

        if order.is_done():
            print(f"\n{'='*55}")
            print(f"  사용자 {user_id} 주문 완료!")
            print(f"{'='*55}")
        else:
            self._show_current(user_id)

    async def start_forward_test(self):
        """포워딩 테스트 - 겹치는 선반이 있는 2개 주문 생성"""
        if self.orders:
            print("  이미 진행 중인 주문이 있습니다. 서버를 재시작하세요.")
            return

        # 두 사용자 모두 선반 11 (1-1) 필요
        result = await self.send({
            "type": "batch_task_request",
            "tasks": [
                {
                    "task_id": "FWD_TEST_1",
                    "workstation_id": 33,
                    "items": ["드롭스", "퍼지"],
                    "optimized_shelf_sequence": [11, 15],
                },
                {
                    "task_id": "FWD_TEST_2",
                    "workstation_id": 34,
                    "items": ["롤리팝", "구미"],
                    "optimized_shelf_sequence": [20, 11],
                },
            ]
        })

        if not result.get("success"):
            print(f"  테스트 작업 생성 실패: {result.get('error')}")
            return

        # UserOrder 설정
        self.orders[1] = UserOrder(1, "FWD_TEST_1", [
            {"order": 1, "shelf_label": "1-1", "shelf_node": 11, "items": ["드롭스"]},
            {"order": 2, "shelf_label": "1-4", "shelf_node": 15, "items": ["퍼지"]},
        ])
        self.orders[2] = UserOrder(2, "FWD_TEST_2", [
            {"order": 1, "shelf_label": "2-2", "shelf_node": 20, "items": ["구미"]},
            {"order": 2, "shelf_label": "1-1", "shelf_node": 11, "items": ["롤리팝"]},
        ])

        print(f"\n{'='*55}")
        print(f"  포워딩 테스트 시작")
        print(f"{'='*55}")
        print(f"  사용자1 (AGV-1, W1): 선반1-1(드롭스) → 선반1-4(퍼지)")
        print(f"  사용자2 (AGV-2, W2): 선반2-2(구미) → 선반1-1(롤리팝)")
        print(f"  *** 선반 1-1 (노드11) 겹침 → 포워딩 발생 예상 ***")
        print(f"{'='*55}")
        print(f"  사용자1 '완료 1' → 선반1-1이 W2로 포워딩되어야 함")
        print(f"  사용자2 '완료 2' → 선반2-2 복귀 후, 포워딩된 선반1-1 자동 수령")
        print(f"{'='*55}")

        for uid in [1, 2]:
            self._show_current(uid)

    async def show_status(self):
        result = await self.send({"type": "status_request"})
        print(f"\n  {'─'*50}")
        print(f"  MQTT: {'연결됨' if result.get('mqtt_connected') else '끊김'}")
        for r in result.get("robots", {}).get("robots", []):
            print(f"  {r['name']}: 노드 {r.get('current_node')}, {r['status']}")
        # 현재 진행 상황
        for uid, order in self.orders.items():
            if not order.is_done():
                shelf = order.current_shelf()
                idx = order.current_shelf_idx + 1
                total = len(order.shelves)
                print(f"  사용자{uid}: [{idx}/{total}] 선반 {shelf['shelf_label']}")
            else:
                print(f"  사용자{uid}: 완료")
        print(f"  {'─'*50}")


def print_help():
    print(f"""
{'='*55}
  AGV 물류 시스템 - CLI 테스트
{'='*55}
  시작 1          사용자1 주문 시작 (AGV-1)
  시작 2          사용자2 주문 시작 (AGV-2)
  완료 1          사용자1 선반 수령 완료 → 다음 선반
  완료 2          사용자2 선반 수령 완료 → 다음 선반
  테스트          포워딩 테스트 (겹치는 선반 주문)
  상태            로봇/주문 상태 조회
  종료            프로그램 종료
{'='*55}""")


async def main():
    client = CLIClient()

    if not await client.connect():
        return

    print_help()

    try:
        while True:
            try:
                cmd = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("\n> ").strip()
                )
            except EOFError:
                break

            if not cmd:
                continue

            if cmd in ("종료", "quit", "q"):
                break

            elif cmd in ("도움", "help", "?"):
                print_help()

            elif cmd.startswith("시작"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    await client.start_order(int(parts[1]))
                else:
                    print("  사용법: 시작 1 또는 시작 2")

            elif cmd.startswith("완료") or cmd.startswith("done"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    await client.shelf_done(int(parts[1]))
                else:
                    print("  사용법: 완료 1 또는 완료 2")

            elif cmd in ("테스트", "test"):
                await client.start_forward_test()

            elif cmd in ("상태", "status", "s"):
                await client.show_status()

            else:
                print(f"  '시작 1/2' / '완료 1/2' / '테스트' / '상태' / '종료'")

    except KeyboardInterrupt:
        print("\n")
    finally:
        if client.ws:
            await client.ws.close()
        print("  종료됨")


if __name__ == "__main__":
    asyncio.run(main())
