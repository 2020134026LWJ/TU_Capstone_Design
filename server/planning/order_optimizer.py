"""
주문 최적화 모듈 (구 task_scheduler)
- 엑셀 주문 목록 로드
- 물품 → 선반 방문 순서 최적화
- AGV 경로 효율화를 위한 재배열

최적화 전략:
1. 같은 선반의 물품은 한 번에 방문
2. AGV 시작 위치에서 가장 가까운 선반부터 방문
3. TSP(외판원 문제) 근사 알고리즘으로 경로 최적화
"""

import json
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from ..data.db_loader import DBLoader

# 노드 좌표의 단일 진실 = map.json.
# (예전엔 여기서 `(id-1) % 8` 로 격자를 역산했다 → 맵 크기나 번호 체계가 바뀌면 조용히 틀린다.
#  map.json에 x/y가 이미 있으므로 그대로 읽는다.)
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DEFAULT_MAP = os.path.join(_DATA_DIR, "map.json")
_DEFAULT_SHELF_CONFIG = os.path.join(_DATA_DIR, "shelf_config.json")


@dataclass
class ScheduledTask:
    """스케줄된 작업 정보"""
    shelf_node: int          # 선반 노드 ID
    shelf_label: str         # 선반 라벨 (예: "1-1")
    items: List[str]         # 해당 선반에서 픽업할 물품들
    order: int               # 방문 순서 (1부터 시작)
    distance_from_start: float  # 시작점으로부터 거리


class OrderOptimizer:
    """주문 최적화 — 물품 → 선반 방문 순서 결정 (Nearest Neighbor)"""

    def __init__(self, db_loader: DBLoader, map_file: Optional[str] = None,
                 shelf_config_file: Optional[str] = None):
        self.db_loader = db_loader
        # 노드 좌표: map.json에서 그대로 읽는다 (격자 수식 역산 금지)
        self.node_coords: Dict[int, Tuple[float, float]] = self._load_node_coords(
            map_file or _DEFAULT_MAP
        )
        # 작업대 노드 → 라벨("W1") — 로그 출력용. 이것도 JSON이 출처.
        self.ws_labels: Dict[int, str] = self._load_ws_labels(
            shelf_config_file or _DEFAULT_SHELF_CONFIG
        )

    @staticmethod
    def _load_node_coords(path: str) -> Dict[int, Tuple[float, float]]:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return {int(n["id"]): (float(n["x"]), float(n["y"])) for n in cfg["nodes"]}

    @staticmethod
    def _load_ws_labels(path: str) -> Dict[int, str]:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return {int(node): info.get("label", f"WS{node}")
                for node, info in cfg.get("workstations", {}).items()}

    def _calc_distance(self, node1: int, node2: int) -> float:
        """두 노드 간 맨해튼 거리 계산"""
        x1, y1 = self.node_coords[node1]
        x2, y2 = self.node_coords[node2]
        return abs(x2 - x1) + abs(y2 - y1)

    def _group_items_by_shelf(self, items: List[str]) -> Dict[int, Dict]:
        """
        물품을 선반별로 그룹화

        Returns:
            {shelf_node: {"label": "1-1", "items": ["물품1", "물품2"]}, ...}
        """
        shelf_groups: Dict[int, Dict] = {}

        for item_name in items:
            shelf_node = self.db_loader.get_item_shelf_node(item_name)
            shelf_label = self.db_loader.get_item_shelf_label(item_name)

            if shelf_node is None:
                print(f"[OrderOptimizer] Warning: '{item_name}' not found in inventory")
                continue

            if shelf_node not in shelf_groups:
                shelf_groups[shelf_node] = {
                    "label": shelf_label,
                    "items": []
                }
            shelf_groups[shelf_node]["items"].append(item_name)

        return shelf_groups

    def optimize_order(
        self,
        items: List[str],
        start_node: int,
        return_to_start: bool = True
    ) -> List[ScheduledTask]:
        """
        물품 피킹 순서 최적화 (Nearest Neighbor 알고리즘)

        Args:
            items: 피킹할 물품 목록
            start_node: AGV 시작 노드 (호출자가 반드시 지정 — 작업대 노드).
                        기본값(옛 33=W1)을 두면 맵이 바뀔 때 조용히 틀린 곳을 기준으로 잡는다.
            return_to_start: 마지막에 시작점으로 복귀 여부

        Returns:
            최적화된 작업 목록 (방문 순서대로)
        """
        # 1. 물품을 선반별로 그룹화
        shelf_groups = self._group_items_by_shelf(items)

        if not shelf_groups:
            print("[OrderOptimizer] No valid items to schedule")
            return []

        # 2. Nearest Neighbor 알고리즘으로 방문 순서 결정
        scheduled: List[ScheduledTask] = []
        remaining_shelves = set(shelf_groups.keys())
        current_node = start_node
        order = 1

        while remaining_shelves:
            # 현재 위치에서 가장 가까운 선반 찾기
            nearest_shelf = None
            min_distance = float('inf')

            for shelf_node in remaining_shelves:
                distance = self._calc_distance(current_node, shelf_node)
                if distance < min_distance:
                    min_distance = distance
                    nearest_shelf = shelf_node

            if nearest_shelf is None:
                break

            # 스케줄에 추가
            shelf_info = shelf_groups[nearest_shelf]
            task = ScheduledTask(
                shelf_node=nearest_shelf,
                shelf_label=shelf_info["label"],
                items=shelf_info["items"],
                order=order,
                distance_from_start=self._calc_distance(start_node, nearest_shelf)
            )
            scheduled.append(task)

            # 다음 반복 준비
            remaining_shelves.remove(nearest_shelf)
            current_node = nearest_shelf
            order += 1

        return scheduled

    def optimize_order_by_distance(
        self,
        items: List[str],
        start_node: int
    ) -> List[ScheduledTask]:
        """
        단순 거리 기반 최적화 (시작점에서 가까운 순)

        Args:
            items: 피킹할 물품 목록
            start_node: AGV 시작 노드

        Returns:
            거리순 정렬된 작업 목록
        """
        shelf_groups = self._group_items_by_shelf(items)

        if not shelf_groups:
            return []

        # 시작점에서의 거리로 정렬
        sorted_shelves = sorted(
            shelf_groups.keys(),
            key=lambda n: self._calc_distance(start_node, n)
        )

        scheduled: List[ScheduledTask] = []
        for order, shelf_node in enumerate(sorted_shelves, 1):
            shelf_info = shelf_groups[shelf_node]
            task = ScheduledTask(
                shelf_node=shelf_node,
                shelf_label=shelf_info["label"],
                items=shelf_info["items"],
                order=order,
                distance_from_start=self._calc_distance(start_node, shelf_node)
            )
            scheduled.append(task)

        return scheduled

    def get_shelf_sequence(self, scheduled_tasks: List[ScheduledTask]) -> List[int]:
        """스케줄된 작업에서 선반 노드 순서만 추출"""
        return [task.shelf_node for task in scheduled_tasks]

    def schedule_order(
        self,
        user_id: int,
        order_id: int,
        optimization: str = "nearest",
        start_node: Optional[int] = None,
    ) -> Optional[Dict]:
        """
        주문 스케줄링 (엑셀에서 로드 → 최적화)

        Args:
            user_id: 사용자 ID (1 또는 2)
            order_id: 주문 번호
            optimization: 최적화 방법 ("nearest", "distance")

        Returns:
            {
                "user_id": 1,
                "order_id": 1,
                "workstation": 50,
                "tasks": [ScheduledTask, ...],
                "shelf_sequence": [9, 23, ...],
                "total_items": 5,
                "total_shelves": 3
            }
        """
        # 1. 주문 정보 로드
        order_info = self.db_loader.get_order(user_id, order_id)
        if not order_info:
            print(f"[OrderOptimizer] Order not found: user={user_id}, order={order_id}")
            return None

        workstation = order_info["workstation_id"]
        items = [item["name"] for item in order_info["items"]]

        # 방문 순서(NN)의 기준점 = AGV가 실제로 출발하는 작업대.
        # 엑셀의 workstation_id는 '사용자의 기본 작업대'라 GUI에서 다른 작업대를 지정하면
        # 어긋난다(작업대-사용자 디커플링, 수정 53). 실제 작업대(start_node)가 오면 그걸 쓴다.
        # 안 오면 엑셀 기본값 폴백.
        origin = start_node if start_node is not None else workstation

        print(f"[OrderOptimizer] Loading order: user={user_id}, order={order_id}")
        print(f"[OrderOptimizer] Items: {items}")

        # 2. 최적화 (origin 기준)
        if optimization == "nearest":
            tasks = self.optimize_order(items, start_node=origin)
        else:
            tasks = self.optimize_order_by_distance(items, start_node=origin)

        if not tasks:
            print(f"[OrderOptimizer] No tasks scheduled for order {order_id}")
            return None

        # 3. 결과 출력
        print(f"[OrderOptimizer] Optimized schedule:")
        for task in tasks:
            print(f"  {task.order}. 선반 {task.shelf_label} (노드 {task.shelf_node}): {task.items}")

        shelf_sequence = self.get_shelf_sequence(tasks)

        return {
            "user_id": user_id,
            "order_id": order_id,
            "workstation": workstation,
            "tasks": tasks,
            "shelf_sequence": shelf_sequence,
            "total_items": len(items),
            "total_shelves": len(tasks),
        }

    def print_schedule(self, schedule: Dict) -> None:
        """스케줄 출력"""
        print("\n" + "=" * 60)
        print(f"  주문 스케줄: 사용자 {schedule['user_id']}, 주문 {schedule['order_id']}")
        print("=" * 60)
        ws = schedule['workstation']
        ws_label = self.ws_labels.get(ws, f"WS{ws}")
        print(f"  작업대: {ws} ({ws_label})")
        print(f"  총 물품: {schedule['total_items']}개")
        print(f"  방문 선반: {schedule['total_shelves']}개")
        print("-" * 60)
        print("  방문 순서:")
        for task in schedule['tasks']:
            print(f"    {task.order}. 선반 {task.shelf_label} (노드 {task.shelf_node})")
            for item in task.items:
                print(f"       - {item}")
        print("-" * 60)
        print(f"  선반 경로: {schedule['shelf_sequence']}")
        print("=" * 60 + "\n")


# 테스트용 실행
if __name__ == "__main__":
    import sys
    # __file__ = server/planning/order_optimizer.py → dirname 3번 = 프로젝트 루트
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, project_root)

    db_dir = os.path.join(project_root, "warehouse_gui_server")
    db_loader = DBLoader(db_dir)
    scheduler = OrderOptimizer(db_loader)

    # 사용자 1의 주문 1 스케줄링
    schedule = scheduler.schedule_order(user_id=1, order_id=1)
    if schedule:
        scheduler.print_schedule(schedule)

    # 사용자 2의 주문 1 스케줄링
    schedule2 = scheduler.schedule_order(user_id=2, order_id=1)
    if schedule2:
        scheduler.print_schedule(schedule2)
