"""
엑셀 주문 데이터 로더
물류계획.xlsx에서 주문 데이터를 읽어 물품 목록을 반환
"""

import os
import re

import openpyxl

# 선반 라벨 → 노드 ID (4×8 그리드)
LABEL_TO_NODE = {
    "1-1": 11, "1-2": 12, "1-3": 14, "1-4": 15,
    "2-1": 19, "2-2": 20, "2-3": 22, "2-4": 23,
}

# 존재하지 않는 선반 → 대체 매핑
LABEL_REMAP = {
    "3-4": "1-3",  # 포도: 시뮬레이션에 3-4 없으므로 1-3(node 14)로 대체
}

# 담당자 → 작업대
WORKER_TO_WS = {
    1: 33,  # W1
    2: 34,  # W2
}

DEFAULT_EXCEL = os.path.join(
    os.path.dirname(__file__),
    "../../docs/종합설계기획/최종 발표 자료/발표 관련 코드/물류계획.xlsx",
)


class DBLoader:
    def __init__(self, excel_path=None):
        self.excel_path = excel_path or DEFAULT_EXCEL
        self.orders = {}  # (user_id, order_num) -> {workstation_id, items}
        self._load()

    def _load(self):
        try:
            wb = openpyxl.load_workbook(self.excel_path)
        except FileNotFoundError:
            print(f"[DBLoader] 엑셀 파일 없음: {self.excel_path}")
            return

        ws = wb["물류계획"]

        for row in ws.iter_rows(min_row=2, values_only=True):
            user_id = int(row[0]) if row[0] else None
            order_num = int(row[1]) if row[1] else None
            if user_id is None or order_num is None:
                continue

            items = []
            for cell_val in row[2:]:
                if cell_val is None:
                    continue
                match = re.match(r"(.+)\((.+)\)", str(cell_val))
                if not match:
                    continue
                item_name = match.group(1)
                shelf_label = match.group(2)
                shelf_label = LABEL_REMAP.get(shelf_label, shelf_label)
                if shelf_label in LABEL_TO_NODE:
                    items.append(item_name)
                else:
                    print(f"[DBLoader] 경고: 알 수 없는 선반 '{shelf_label}' (물품: {item_name})")

            self.orders[(user_id, order_num)] = {
                "workstation_id": WORKER_TO_WS.get(user_id, 33),
                "items": items,
            }

        print(f"[DBLoader] {len(self.orders)}개 주문 로드 완료")

    def get_order(self, user_id, order_num=1):
        """주문 조회 → {workstation_id, items} 또는 None"""
        return self.orders.get((user_id, order_num))

    def get_order_count(self, user_id):
        """사용자의 총 주문 수"""
        return sum(1 for (uid, _) in self.orders if uid == user_id)

    def list_orders(self):
        """전체 주문 출력"""
        for (uid, onum), order in sorted(self.orders.items()):
            ws = "W1" if order["workstation_id"] == 33 else "W2"
            items_str = ", ".join(order["items"])
            print(f"  담당자{uid} 작업{onum} ({ws}): {items_str}")


if __name__ == "__main__":
    loader = DBLoader()
    loader.list_orders()
