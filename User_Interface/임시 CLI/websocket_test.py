"""
WebSocket 테스트 클라이언트 - CLI 기반 UI 시뮬레이터

사용법:
    python websocket_test.py

흐름:
    > 시작 1           사용자1 주문 시작 (엑셀에서 자동 로드)
    > 시작 2           사용자2 주문 시작
    (AGV가 각각 선반 가져오면)
    > 완료 1           사용자1 선반 수령 완료 → 다음 선반
    > 완료 2           사용자2 선반 수령 완료 → 다음 선반
"""

import asyncio
import json
import websockets

from db_loader import DBLoader, LABEL_TO_NODE

WS_URL = "ws://localhost:8765"

# 선반 노드 → 라벨 (역매핑)
NODE_TO_LABEL = {v: k for k, v in LABEL_TO_NODE.items()}


class UserOrder:
    """사용자별 주문 상태"""

    def __init__(self, user_id, task_id, shelves):
        self.user_id = user_id
        self.task_id = task_id
        self.shelves = shelves    # [{"order", "shelf_label", "shelf_node", "items"}, ...]
        self.current_shelf_idx = 0
        self.picked_items = set()  # 현재 선반에서 완료된 물품

    def is_done(self):
        return self.current_shelf_idx >= len(self.shelves)

    def current_shelf(self):
        if self.is_done():
            return None
        return self.shelves[self.current_shelf_idx]

    def get_next_unpicked_item(self):
        """현재 선반에서 아직 완료하지 않은 다음 물품"""
        shelf = self.current_shelf()
        if not shelf:
            return None
        for item in shelf["items"]:
            if item not in self.picked_items:
                return item
        return None

    def get_remaining_items(self):
        """현재 선반에서 남은 물품 목록"""
        shelf = self.current_shelf()
        if not shelf:
            return []
        return [item for item in shelf["items"] if item not in self.picked_items]

    def advance(self):
        self.current_shelf_idx += 1
        self.picked_items.clear()


class CLIClient:

    def __init__(self):
        self.ws = None
        self.orders = {}  # user_id → UserOrder
        self.db_loader = DBLoader()
        self.order_counter = {}  # user_id → 다음 주문 번호

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
        """주문 시작 - 엑셀에서 주문 로드 → batch_task_request 전송"""
        if user_id in self.orders and not self.orders[user_id].is_done():
            print(f"  사용자 {user_id}의 주문이 이미 진행 중입니다.")
            return

        # 다음 주문 번호
        order_num = self.order_counter.get(user_id, 0) + 1
        order_data = self.db_loader.get_order(user_id, order_num)

        if not order_data:
            total = self.db_loader.get_order_count(user_id)
            if total == 0:
                print(f"  사용자 {user_id}의 주문이 엑셀에 없습니다.")
            else:
                print(f"  사용자 {user_id}의 주문을 모두 완료했습니다. ({total}개)")
            return

        self.order_counter[user_id] = order_num

        task_id = f"ORDER_{user_id}_{order_num}"
        result = await self.send({
            "type": "batch_task_request",
            "tasks": [{
                "task_id": task_id,
                "workstation_id": order_data["workstation_id"],
                "items": order_data["items"],
            }]
        })

        if not result.get("success"):
            print(f"  주문 실패: {result.get('error')}")
            return

        # 서버 응답에서 선반 시퀀스 추출
        task_info = result.get("tasks", [{}])[0]
        shelves_needed = task_info.get("shelves_needed", [])

        # 서버가 결정한 선반 순서 기반으로 물품-선반 매핑 구성
        # shelf_manager가 물품→선반을 매핑하므로, 서버 응답의 선반 순서를 따름
        shelves = self._build_shelf_list(shelves_needed, order_data["items"])

        self.orders[user_id] = UserOrder(user_id, task_id, shelves)

        ws_label = "W1" if order_data["workstation_id"] == 33 else "W2"
        print(f"\n{'='*55}")
        print(f"  사용자 {user_id} 작업 {order_num} 시작 ({ws_label})")
        print(f"{'='*55}")
        print(f"  필요 물품: {', '.join(order_data['items'])}")
        print(f"  선반 방문 순서: {' → '.join(NODE_TO_LABEL.get(s, str(s)) for s in shelves_needed)}")
        for shelf in shelves:
            print(f"    {shelf['shelf_label']}: {', '.join(shelf['items'])}")
        print(f"{'='*55}")

        self._show_current(user_id)

    def _build_shelf_list(self, shelves_needed, all_items):
        """서버가 결정한 선반 순서에 맞춰 선반별 물품 목록 구성"""
        # 물품 → 선반 노드 역매핑 (shelf_config 기반으로 서버가 배정한 것을 추정)
        # 서버의 shelf_manager와 동일한 매핑을 사용
        shelves = []
        assigned_items = set()

        for i, shelf_node in enumerate(shelves_needed):
            shelf_label = NODE_TO_LABEL.get(shelf_node, f"S{shelf_node}")
            # 이 선반에 해당하는 물품 찾기 (db_loader의 매핑 활용)
            items_on_shelf = []
            for item in all_items:
                if item not in assigned_items:
                    # 물품의 선반을 db_loader에서 확인
                    item_shelf = self._get_item_shelf_node(item)
                    if item_shelf == shelf_node:
                        items_on_shelf.append(item)
                        assigned_items.add(item)

            shelves.append({
                "order": i + 1,
                "shelf_label": shelf_label,
                "shelf_node": shelf_node,
                "items": items_on_shelf,
            })

        return shelves

    def _get_item_shelf_node(self, item_name):
        """물품명 → 선반 노드 (shelf_config.json 기반)"""
        ITEM_TO_NODE = {
            "딸기": 11, "사과": 12, "포도": 14,
            "체리": 20, "메론": 22,
        }
        return ITEM_TO_NODE.get(item_name)

    def _show_current(self, user_id):
        """현재 선반 상태 표시"""
        order = self.orders.get(user_id)
        if not order or order.is_done():
            return

        shelf = order.current_shelf()
        idx = order.current_shelf_idx + 1
        total = len(order.shelves)
        remaining = order.get_remaining_items()
        items_str = ", ".join(remaining)

        print(f"\n  [사용자{user_id}] [{idx}/{total}] AGV가 선반 {shelf['shelf_label']}을 가져오는 중...")
        print(f"           물품: {items_str}")
        if remaining:
            print(f"           '완료 {user_id}' → [{remaining[0]}] 완료 처리")

    async def shelf_done(self, user_id):
        """물품 1개 완료 처리 (개별 물품 단위)"""
        order = self.orders.get(user_id)
        if not order:
            print(f"  사용자 {user_id}의 진행 중인 주문이 없습니다.")
            return

        if order.is_done():
            print(f"  사용자 {user_id}의 주문은 이미 완료됨")
            return

        shelf = order.current_shelf()
        item = order.get_next_unpicked_item()

        if not item:
            print(f"  [사용자{user_id}] 현재 선반의 물품을 모두 완료했습니다.")
            return

        result = await self.send({
            "type": "pick_complete",
            "task_id": order.task_id,
            "item": item,
        })

        if not result.get("success"):
            error = result.get("error", "")
            if "WAIT_PICKING" in error:
                print(f"  [사용자{user_id}] AGV가 아직 선반을 가져오는 중입니다. 잠시 기다려주세요.")
            else:
                print(f"  [사용자{user_id}] 오류: {error}")
            return

        order.picked_items.add(item)
        remaining = order.get_remaining_items()
        action = result.get("action", "")
        next_action = result.get("next_action", "")

        if action == "continue_picking":
            # 아직 이 선반에서 더 꺼낼 물품이 있음
            print(f"  [사용자{user_id}] [{item}] 완료! 남은 물품: {', '.join(remaining)}")
            if remaining:
                print(f"           '완료 {user_id}' → [{remaining[0]}] 완료 처리")
        else:
            # 선반의 모든 물품 완료 → 선반 복귀/포워딩
            print(f"  [사용자{user_id}] [{item}] 완료! 선반 {shelf['shelf_label']} 물품 모두 완료")

            if next_action == "forward_shelf":
                forward_ws = result.get("forward_to_ws", "?")
                ws_label = "W1" if forward_ws == 33 else "W2" if forward_ws == 34 else f"WS{forward_ws}"
                print(f"           → 다른 작업대({ws_label})에서도 필요! 포워딩 중...")
            else:
                print(f"           → 선반 복귀 중...")

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

        # 두 사용자 모두 메론(선반2-3, 노드22) 필요
        result = await self.send({
            "type": "batch_task_request",
            "tasks": [
                {
                    "task_id": "FWD_TEST_1",
                    "workstation_id": 33,
                    "items": ["사과", "메론"],
                },
                {
                    "task_id": "FWD_TEST_2",
                    "workstation_id": 34,
                    "items": ["딸기", "메론"],
                },
            ]
        })

        if not result.get("success"):
            print(f"  테스트 작업 생성 실패: {result.get('error')}")
            return

        self.orders[1] = UserOrder(1, "FWD_TEST_1", [
            {"order": 1, "shelf_label": "1-2", "shelf_node": 12, "items": ["사과"]},
            {"order": 2, "shelf_label": "2-3", "shelf_node": 22, "items": ["메론"]},
        ])
        self.orders[2] = UserOrder(2, "FWD_TEST_2", [
            {"order": 1, "shelf_label": "1-1", "shelf_node": 11, "items": ["딸기"]},
            {"order": 2, "shelf_label": "2-3", "shelf_node": 22, "items": ["메론"]},
        ])

        print(f"\n{'='*55}")
        print(f"  포워딩 테스트 시작")
        print(f"{'='*55}")
        print(f"  사용자1 (W1): 선반1-2(사과) → 선반2-3(메론)")
        print(f"  사용자2 (W2): 선반1-1(딸기) → 선반2-3(메론)")
        print(f"  *** 선반 2-3 (메론) 겹침 → 포워딩 발생 예상 ***")
        print(f"{'='*55}")

        for uid in [1, 2]:
            self._show_current(uid)

    async def show_status(self):
        result = await self.send({"type": "status_request"})
        print(f"\n  {'─'*50}")
        print(f"  MQTT: {'연결됨' if result.get('mqtt_connected') else '끊김'}")
        for r in result.get("robots", {}).get("robots", []):
            shelf_info = f", 선반 {NODE_TO_LABEL.get(r['carrying_shelf'], r['carrying_shelf'])}" if r.get('carrying_shelf') else ""
            print(f"  {r['name']}: 노드 {r.get('current_node')}, {r['status']}{shelf_info}")
        for uid, order in self.orders.items():
            if not order.is_done():
                shelf = order.current_shelf()
                idx = order.current_shelf_idx + 1
                total = len(order.shelves)
                print(f"  사용자{uid}: [{idx}/{total}] 선반 {shelf['shelf_label']}")
            else:
                print(f"  사용자{uid}: 완료")
        print(f"  {'─'*50}")

    def show_orders(self):
        """엑셀 주문 목록 표시"""
        print(f"\n  {'─'*50}")
        print(f"  엑셀 주문 목록 (물류계획.xlsx)")
        print(f"  {'─'*50}")
        self.db_loader.list_orders()
        print(f"  {'─'*50}")


def print_help():
    print(f"""
{'='*55}
  AGV 물류 시스템 - CLI 테스트
{'='*55}
  시작 1          사용자1 주문 시작 (엑셀에서 로드)
  시작 2          사용자2 주문 시작 (엑셀에서 로드)
  완료 1          사용자1 물품 1개 완료 (개별 처리)
  완료 2          사용자2 물품 1개 완료 (개별 처리)
  테스트          포워딩 테스트 (겹치는 선반 주문)
  주문목록        엑셀 주문 목록 확인
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

            elif cmd in ("주문목록", "orders"):
                client.show_orders()

            elif cmd in ("상태", "status", "s"):
                await client.show_status()

            else:
                print(f"  '시작 1/2' / '완료 1/2' / '테스트' / '주문목록' / '상태' / '종료'")

    except KeyboardInterrupt:
        print("\n")
    finally:
        if client.ws:
            await client.ws.close()
        print("  종료됨")


if __name__ == "__main__":
    asyncio.run(main())
