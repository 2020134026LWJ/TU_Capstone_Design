"""
선반 관리 모듈
- 선반 상태 추적 (위치, 물품, 운반 여부)
- 물품 → 선반 매핑
- 빈 선반 위치 탐색
"""

import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any


class ShelfStatus(Enum):
    """선반 상태"""
    IN_PLACE = "in_place"           # 원래 위치에 있음
    CARRIED = "carried"             # 로봇이 운반 중
    AT_WORKSTATION = "at_workstation"  # 작업대에 있음


@dataclass
class Shelf:
    """선반 정보"""
    shelf_id: int                   # 홈 노드 ID (예: 9)
    label: str                      # 라벨 (예: "S1")
    items: List[str]                # 보유 물품 (예: ["A", "B", "C"])
    home_node: int                  # 원래 위치 노드
    current_node: int               # 현재 위치 노드
    status: ShelfStatus = ShelfStatus.IN_PLACE
    carried_by: Optional[int] = None  # 운반 중인 로봇 ID

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shelf_id": self.shelf_id,
            "label": self.label,
            "items": self.items,
            "home_node": self.home_node,
            "current_node": self.current_node,
            "status": self.status.value,
            "carried_by": self.carried_by,
        }


class ShelfManager:
    """선반 관리자"""

    def __init__(self, shelf_config_file: str):
        self.shelf_config_file = shelf_config_file
        self.shelves: Dict[int, Shelf] = {}          # shelf_id -> Shelf
        self.item_to_shelf: Dict[str, int] = {}      # item_name -> shelf_id
        self.all_shelf_nodes: Set[int] = set()        # 모든 선반 노드 ID
        self.workstations: Dict[int, Dict] = {}       # ws_node -> config
        self._load_config()

    def _load_config(self) -> None:
        """shelf_config.json 로드"""
        try:
            with open(self.shelf_config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            for shelf_id_str, info in data.get("shelves", {}).items():
                shelf_id = int(shelf_id_str)
                shelf = Shelf(
                    shelf_id=shelf_id,
                    label=info.get("label", f"S{shelf_id}"),
                    items=info.get("items", []),
                    home_node=shelf_id,
                    current_node=shelf_id,
                )
                self.shelves[shelf_id] = shelf
                self.all_shelf_nodes.add(shelf_id)

                for item in shelf.items:
                    self.item_to_shelf[item] = shelf_id

            for ws_id_str, ws_info in data.get("workstations", {}).items():
                self.workstations[int(ws_id_str)] = ws_info

            print(f"[ShelfManager] Loaded {len(self.shelves)} shelves, "
                  f"{len(self.item_to_shelf)} items, "
                  f"{len(self.workstations)} workstations")

        except FileNotFoundError:
            print(f"[ShelfManager] Config not found: {self.shelf_config_file}")
        except Exception as e:
            print(f"[ShelfManager] Error loading config: {e}")

    def get_shelf(self, shelf_id: int) -> Optional[Shelf]:
        """선반 조회"""
        return self.shelves.get(shelf_id)

    def get_shelf_by_item(self, item: str) -> Optional[Shelf]:
        """물품으로 선반 찾기"""
        shelf_id = self.item_to_shelf.get(item)
        if shelf_id is not None:
            return self.shelves.get(shelf_id)
        return None

    def find_shelves_for_items(self, items: List[str]) -> Dict[int, List[str]]:
        """
        필요 물품 리스트 → {shelf_id: [해당 선반에서 가져올 물품들]}
        """
        result: Dict[int, List[str]] = {}
        for item in items:
            shelf_id = self.item_to_shelf.get(item)
            if shelf_id is not None:
                if shelf_id not in result:
                    result[shelf_id] = []
                result[shelf_id].append(item)
            else:
                print(f"[ShelfManager] Warning: item '{item}' not found on any shelf")
        return result

    def mark_shelf_picked_up(self, shelf_id: int, robot_id: int) -> bool:
        """선반을 로봇이 들어올림"""
        shelf = self.shelves.get(shelf_id)
        if not shelf:
            return False
        shelf.status = ShelfStatus.CARRIED
        shelf.carried_by = robot_id
        print(f"[ShelfManager] Shelf {shelf.label} picked up by Robot {robot_id}")
        return True

    def mark_shelf_at_workstation(self, shelf_id: int, ws_node: int) -> bool:
        """선반이 작업대에 도착"""
        shelf = self.shelves.get(shelf_id)
        if not shelf:
            return False
        shelf.status = ShelfStatus.AT_WORKSTATION
        shelf.current_node = ws_node
        print(f"[ShelfManager] Shelf {shelf.label} at workstation node {ws_node}")
        return True

    def mark_shelf_returned(self, shelf_id: int, return_node: int) -> bool:
        """선반을 원래 위치(또는 빈 자리)에 복귀"""
        shelf = self.shelves.get(shelf_id)
        if not shelf:
            return False
        shelf.status = ShelfStatus.IN_PLACE
        shelf.current_node = return_node
        shelf.carried_by = None
        print(f"[ShelfManager] Shelf {shelf.label} returned to node {return_node}")
        return True

    def get_empty_shelf_positions(self) -> List[int]:
        """현재 비어있는 선반 자리 반환"""
        occupied = set()
        for shelf in self.shelves.values():
            if shelf.status == ShelfStatus.IN_PLACE:
                occupied.add(shelf.current_node)
        return [node for node in self.all_shelf_nodes if node not in occupied]

    def find_nearest_empty_position(self, from_node: int, path_planner) -> Optional[int]:
        """from_node에서 가장 가까운 빈 선반 자리 찾기 (유클리드 거리 기준)"""
        empty = self.get_empty_shelf_positions()
        if not empty:
            return None

        best_node = None
        best_dist = float("inf")
        for node in empty:
            dist = path_planner._heuristic(from_node, node)
            if dist < best_dist:
                best_dist = dist
                best_node = node
        return best_node

    def get_all_shelf_nodes(self) -> Set[int]:
        """모든 선반 노드 ID 집합"""
        return self.all_shelf_nodes.copy()

    def get_status_summary(self) -> Dict[str, Any]:
        """전체 선반 상태 요약"""
        return {
            "total_shelves": len(self.shelves),
            "in_place": sum(1 for s in self.shelves.values() if s.status == ShelfStatus.IN_PLACE),
            "carried": sum(1 for s in self.shelves.values() if s.status == ShelfStatus.CARRIED),
            "at_workstation": sum(1 for s in self.shelves.values() if s.status == ShelfStatus.AT_WORKSTATION),
            "shelves": [s.to_dict() for s in self.shelves.values()],
        }
