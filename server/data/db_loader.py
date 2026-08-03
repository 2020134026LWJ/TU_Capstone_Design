"""
엑셀 DB 로더 모듈
- 주문 정보 로드 (사용자1주문.xlsx, 사용자2주문.xlsx)
- 재고 정보 로드/수정 (데이터 베이스.xlsx)
- 물품명 → 선반 노드 매핑
"""

import json
import os
import pandas as pd
from typing import Dict, List, Optional, Tuple

# 선반 라벨↔노드 매핑의 단일 진실 = shelf_config.json.
# (예전엔 이 파일에도 같은 표가 복붙돼 있어서, 맵을 바꾸면 두 곳을 고쳐야 했다.
#  한쪽만 고치면 "물품은 있는데 엉뚱한 선반으로 간다"가 조용히 발생.)
_DEFAULT_SHELF_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "shelf_config.json")


class DBLoader:
    """엑셀 DB 로더"""

    def __init__(self, db_dir: str, shelf_config_file: Optional[str] = None):
        self.db_dir = db_dir
        self.inventory_file = os.path.join(db_dir, "데이터 베이스.xlsx")
        self.order_files = {
            1: os.path.join(db_dir, "사용자1주문.xlsx"),
            2: os.path.join(db_dir, "사용자2주문.xlsx"),
        }

        # 선반 라벨("1-1") → 노드 ID, 사용자 → 작업대 노드. 둘 다 JSON에서 읽는다 (하드코딩 금지).
        self.shelf_node_map, self.user_to_ws_node = self._load_shelf_config(
            shelf_config_file or _DEFAULT_SHELF_CONFIG
        )

        # 캐시
        self._inventory_cache: Optional[pd.DataFrame] = None
        self._item_to_shelf: Dict[str, Tuple[str, int]] = {}  # 물품명 → (선반번호, 노드ID)

        self._load_inventory()

    @staticmethod
    def _load_shelf_config(path: str) -> Tuple[Dict[str, int], Dict[int, int]]:
        """shelf_config.json에서 (선반 라벨→노드, 사용자→작업대 노드)를 읽는다.

        맵/번호 체계가 바뀌어도 고칠 곳은 이 JSON 하나뿐이게 하기 위함.
        """
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)

        shelf_node_map = {label: int(node) for label, node in cfg.get("shelf_node_map", {}).items()}
        if not shelf_node_map:
            raise ValueError(f"[DBLoader] shelf_node_map이 비어있음: {path}")

        user_to_ws_node: Dict[int, int] = {}
        for ws_node, info in cfg.get("workstations", {}).items():
            user_id = info.get("user_id")
            if user_id is not None:
                user_to_ws_node[int(user_id)] = int(ws_node)

        return shelf_node_map, user_to_ws_node

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
                node_id = self.shelf_node_map.get(shelf_key)
                if node_id is not None:      # 노드 0도 유효한 노드다 (0-based 번호 체계)
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
            df = df.iloc[:, :3]  # 스프레드시트 편집으로 붙는 유령 빈 컬럼 방어
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

        # 작업대 폴백: 사용자 → 작업대 역산 (shelf_config.json의 user_id 필드 기준).
        # [주의] 이건 '작업대' 필드가 없는 옛 메시지용 폴백일 뿐이다 (수정 53).
        # 정상 경로에서는 GUI가 보낸 '작업대'를 서버가 그대로 쓴다 — 사용자는 자리를 옮길 수 있으니까.
        workstation_id = self.user_to_ws_node.get(user_id)
        if workstation_id is None:
            print(f"[DBLoader] user {user_id}에 대응하는 작업대가 shelf_config.json에 없음")
            return None

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
            df = df.iloc[:, :3]  # 스프레드시트 편집으로 붙는 유령 빈 컬럼 방어
            df.columns = ["주문", "물건", "개수"]
            df = df.iloc[1:]

        return sorted(df["주문"].unique().tolist())
