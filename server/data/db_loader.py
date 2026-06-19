"""
엑셀 DB 로더 모듈
- 주문 정보 로드 (사용자1주문.xlsx, 사용자2주문.xlsx)
- 재고 정보 로드/수정 (데이터 베이스.xlsx)
- 물품명 → 선반 노드 매핑
"""

import os
import pandas as pd
from typing import Dict, List, Optional, Tuple


class DBLoader:
    """엑셀 DB 로더"""

    # 선반 번호 → 노드 ID 매핑 (6×8 그리드 기준)
    SHELF_NODE_MAP = {
        "1-1": 19, "1-2": 20, "1-3": 27, "1-4": 28,
        "2-1": 22, "2-2": 23, "2-3": 30, "2-4": 31,
    }

    def __init__(self, db_dir: str):
        self.db_dir = db_dir
        self.inventory_file = os.path.join(db_dir, "데이터 베이스.xlsx")
        self.order_files = {
            1: os.path.join(db_dir, "사용자1주문.xlsx"),
            2: os.path.join(db_dir, "사용자2주문.xlsx"),
        }

        # 캐시
        self._inventory_cache: Optional[pd.DataFrame] = None
        self._item_to_shelf: Dict[str, Tuple[str, int]] = {}  # 물품명 → (선반번호, 노드ID)

        self._load_inventory()

    def _load_inventory(self) -> None:
        """재고 DB 로드 및 물품→선반 매핑 생성"""
        if not os.path.exists(self.inventory_file):
            print(f"[DBLoader] Inventory file not found: {self.inventory_file}")
            return

        df = pd.read_excel(self.inventory_file)
        self._inventory_cache = df

        # 물품명 → 선반 매핑 생성
        for _, row in df.iterrows():
            shelf_full = str(row.get("선반 번호", ""))  # "1-1-1"
            item_name = str(row.get("물건", "")).strip()

            if not shelf_full or not item_name or item_name == "nan":
                continue

            # "1-1-1" → "1-1" (선반-층만 사용, 칸은 무시)
            parts = shelf_full.split("-")
            if len(parts) >= 2:
                shelf_key = f"{parts[0]}-{parts[1]}"
                node_id = self.SHELF_NODE_MAP.get(shelf_key)
                if node_id:
                    self._item_to_shelf[item_name] = (shelf_key, node_id)

        print(f"[DBLoader] Loaded {len(self._item_to_shelf)} items from inventory")

    def get_order(self, user_id: int, order_id: int) -> Optional[Dict]:
        """
        주문 정보 조회

        Returns:
            {
                "workstation_id": 50,
                "items": [{"name": "드롭스", "quantity": 3}, ...]
            }
        """
        order_file = self.order_files.get(user_id)
        if not order_file or not os.path.exists(order_file):
            print(f"[DBLoader] Order file not found for user {user_id}")
            return None

        df = pd.read_excel(order_file)

        # 컬럼명 정리 (첫 행이 헤더인 경우)
        if df.columns[0].startswith("사용자"):
            df.columns = ["주문", "물건", "개수"]
            df = df.iloc[1:]  # 첫 행 제거

        # 주문번호로 필터링
        order_df = df[df["주문"] == order_id]

        if order_df.empty:
            print(f"[DBLoader] Order {order_id} not found for user {user_id}")
            return None

        items = []
        for _, row in order_df.iterrows():
            item_name = str(row["물건"]).strip()
            quantity = int(row["개수"])
            items.append({"name": item_name, "quantity": quantity})

        # 작업대: 사용자1 → W1(33), 사용자2 → W2(9)
        workstation_id = 33 if user_id == 1 else 9

        return {
            "workstation_id": workstation_id,
            "items": items,
        }

    def get_item_shelf_node(self, item_name: str) -> Optional[int]:
        """물품명 → 선반 노드 ID"""
        result = self._item_to_shelf.get(item_name)
        return result[1] if result else None

    def get_item_shelf_label(self, item_name: str) -> Optional[str]:
        """물품명 → 선반 라벨 (예: "1-1")"""
        result = self._item_to_shelf.get(item_name)
        return result[0] if result else None

    def get_items_with_shelves(self, item_names: List[str]) -> List[Dict]:
        """
        물품 목록에 대해 선반 정보 추가

        Returns:
            [{"name": "드롭스", "shelf_node": 9, "shelf_label": "1-1"}, ...]
        """
        result = []
        for name in item_names:
            shelf_info = self._item_to_shelf.get(name)
            if shelf_info:
                result.append({
                    "name": name,
                    "shelf_node": shelf_info[1],
                    "shelf_label": shelf_info[0],
                })
            else:
                print(f"[DBLoader] Item '{name}' not found in inventory")
                result.append({
                    "name": name,
                    "shelf_node": None,
                    "shelf_label": None,
                })
        return result

    def get_shelves_for_items(self, item_names: List[str]) -> List[int]:
        """
        물품 목록 → 필요한 선반 노드 목록 (중복 제거, 순서 유지)
        """
        seen = set()
        shelves = []
        for name in item_names:
            node = self.get_item_shelf_node(name)
            if node and node not in seen:
                seen.add(node)
                shelves.append(node)
        return shelves

    def update_inventory(self, item_name: str, quantity_change: int) -> bool:
        """
        재고 수정

        Args:
            item_name: 물품명
            quantity_change: 변경량 (음수면 감소)

        Returns:
            성공 여부
        """
        if self._inventory_cache is None:
            return False

        # 해당 물품 찾기
        mask = self._inventory_cache["물건"] == item_name
        if not mask.any():
            print(f"[DBLoader] Item '{item_name}' not found for inventory update")
            return False

        # 재고 수정
        idx = mask.idxmax()
        current = self._inventory_cache.loc[idx, "재고"]
        new_value = max(0, current + quantity_change)
        self._inventory_cache.loc[idx, "재고"] = new_value

        # 파일에 저장
        try:
            self._inventory_cache.to_excel(self.inventory_file, index=False)
            print(f"[DBLoader] Updated '{item_name}': {current} → {new_value}")
            return True
        except Exception as e:
            print(f"[DBLoader] Failed to save inventory: {e}")
            return False

    def get_inventory_status(self, item_name: str) -> Optional[int]:
        """물품 재고 조회"""
        if self._inventory_cache is None:
            return None

        mask = self._inventory_cache["물건"] == item_name
        if mask.any():
            return int(self._inventory_cache.loc[mask.idxmax(), "재고"])
        return None

    def get_all_orders(self, user_id: int) -> List[int]:
        """사용자의 모든 주문번호 목록"""
        order_file = self.order_files.get(user_id)
        if not order_file or not os.path.exists(order_file):
            return []

        df = pd.read_excel(order_file)

        if df.columns[0].startswith("사용자"):
            df.columns = ["주문", "물건", "개수"]
            df = df.iloc[1:]

        return sorted(df["주문"].unique().tolist())
