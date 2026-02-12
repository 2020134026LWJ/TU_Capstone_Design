"""
작업 스케줄러 모듈
- 엑셀 주문 목록 로드
- 물품 → 선반 방문 순서 최적화
- AGV 경로 효율화를 위한 재배열

최적화 전략:
1. 같은 선반의 물품은 한 번에 방문
2. AGV 시작 위치에서 가장 가까운 선반부터 방문
3. TSP(외판원 문제) 근사 알고리즘으로 경로 최적화
"""

import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from .db_loader import DBLoader


@dataclass
class ScheduledTask:
    """스케줄된 작업 정보"""
    shelf_node: int          # 선반 노드 ID
    shelf_label: str         # 선반 라벨 (예: "1-1")
    items: List[str]         # 해당 선반에서 픽업할 물품들
    order: int               # 방문 순서 (1부터 시작)
    distance_from_start: float  # 시작점으로부터 거리


class TaskScheduler:
    """작업 스케줄러 - 물품 피킹 순서 최적화"""

    # 노드 좌표 (맵 기준, 4x8 그리드)
    NODE_COORDS = {
        # 그리드 노드 (1-32)
        **{i: ((i-1) % 8, (i-1) // 8) for i in range(1, 33)},
        # 작업대 노드
        33: (-1, 0),   # W1
        34: (-1, 3),   # W2
    }

    # 선반 노드 목록
    SHELF_NODES = {11, 12, 14, 15, 19, 20, 22, 23}

    def __init__(self, db_loader: DBLoader):
        self.db_loader = db_loader

    def _calc_distance(self, node1: int, node2: int) -> float:
        """두 노드 간 맨해튼 거리 계산"""
        x1, y1 = self.NODE_COORDS.get(node1, (0, 0))
        x2, y2 = self.NODE_COORDS.get(node2, (0, 0))
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
                print(f"[TaskScheduler] Warning: '{item_name}' not found in inventory")
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
        start_node: int = 33,
        return_to_start: bool = True
    ) -> List[ScheduledTask]:
        """
        물품 피킹 순서 최적화 (Nearest Neighbor 알고리즘)

        Args:
            items: 피킹할 물품 목록
            start_node: AGV 시작 노드 (기본: 33=W1)
            return_to_start: 마지막에 시작점으로 복귀 여부

        Returns:
            최적화된 작업 목록 (방문 순서대로)
        """
        # 1. 물품을 선반별로 그룹화
        shelf_groups = self._group_items_by_shelf(items)

        if not shelf_groups:
            print("[TaskScheduler] No valid items to schedule")
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
        start_node: int = 33
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
        optimization: str = "nearest"
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
            print(f"[TaskScheduler] Order not found: user={user_id}, order={order_id}")
            return None

        workstation = order_info["workstation_id"]
        items = [item["name"] for item in order_info["items"]]

        print(f"[TaskScheduler] Loading order: user={user_id}, order={order_id}")
        print(f"[TaskScheduler] Items: {items}")

        # 2. 최적화
        if optimization == "nearest":
            tasks = self.optimize_order(items, start_node=workstation)
        else:
            tasks = self.optimize_order_by_distance(items, start_node=workstation)

        if not tasks:
            print(f"[TaskScheduler] No tasks scheduled for order {order_id}")
            return None

        # 3. 결과 출력
        print(f"[TaskScheduler] Optimized schedule:")
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
        print(f"  작업대: {schedule['workstation']} (W{'1' if schedule['workstation'] == 33 else '2'})")
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
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Database")
    db_loader = DBLoader(db_dir)
    scheduler = TaskScheduler(db_loader)

    # 사용자 1의 주문 1 스케줄링
    schedule = scheduler.schedule_order(user_id=1, order_id=1)
    if schedule:
        scheduler.print_schedule(schedule)

    # 사용자 2의 주문 1 스케줄링
    schedule2 = scheduler.schedule_order(user_id=2, order_id=1)
    if schedule2:
        scheduler.print_schedule(schedule2)
