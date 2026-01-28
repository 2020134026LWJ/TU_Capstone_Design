"""
작업 관리 모듈
- 작업 일괄 등록 및 분해
- 서브태스크 시퀀싱 (선반 픽업 → 배달 → 대기 → 복귀)
- 픽업 완료 처리
- 선반 간 포워딩 (다른 작업대에서도 필요한 경우)
"""

import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


class TaskStatus(Enum):
    """작업 상태"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class SubTaskType(Enum):
    """서브태스크 유형"""
    GO_TO_SHELF = "go_to_shelf"             # 선반 위치로 이동
    PICKUP_SHELF = "pickup_shelf"           # 선반 들어올리기
    DELIVER_TO_WS = "deliver_to_ws"         # 작업대로 배달
    WAIT_PICKING = "wait_picking"           # 작업자 픽업 대기
    RETURN_SHELF = "return_shelf"           # 선반 복귀 (빈 자리로)
    FORWARD_SHELF = "forward_shelf"         # 다른 작업대로 포워딩


@dataclass
class SubTask:
    """서브태스크"""
    subtask_id: str
    subtask_type: SubTaskType
    shelf_id: Optional[int]                 # 대상 선반
    target_node: int                        # 이동 목표 노드
    items_to_pick: List[str] = field(default_factory=list)  # 이 단계에서 픽업할 물품
    status: TaskStatus = TaskStatus.PENDING
    assigned_robot: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "subtask_type": self.subtask_type.value,
            "shelf_id": self.shelf_id,
            "target_node": self.target_node,
            "items_to_pick": self.items_to_pick,
            "status": self.status.value,
            "assigned_robot": self.assigned_robot,
        }


@dataclass
class PickingTask:
    """피킹 작업"""
    task_id: str
    workstation_id: int                     # 요청 작업대 노드 (50 or 51)
    items: List[str]                        # 전체 필요 물품
    shelf_sequence: List[int]               # 방문할 선반 순서
    subtasks: List[SubTask] = field(default_factory=list)
    current_subtask_idx: int = 0
    status: TaskStatus = TaskStatus.PENDING
    items_picked: List[str] = field(default_factory=list)
    assigned_robot: Optional[int] = None
    created_at: float = field(default_factory=time.time)

    def get_current_subtask(self) -> Optional[SubTask]:
        """현재 서브태스크 반환"""
        if 0 <= self.current_subtask_idx < len(self.subtasks):
            return self.subtasks[self.current_subtask_idx]
        return None

    def advance_subtask(self) -> Optional[SubTask]:
        """다음 서브태스크로 진행"""
        if self.current_subtask_idx < len(self.subtasks):
            self.subtasks[self.current_subtask_idx].status = TaskStatus.COMPLETED
        self.current_subtask_idx += 1
        nxt = self.get_current_subtask()
        if nxt:
            nxt.status = TaskStatus.IN_PROGRESS
            return nxt
        return None

    def is_complete(self) -> bool:
        """작업 완료 여부"""
        return self.current_subtask_idx >= len(self.subtasks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workstation_id": self.workstation_id,
            "items": self.items,
            "items_picked": self.items_picked,
            "shelf_sequence": self.shelf_sequence,
            "current_subtask_idx": self.current_subtask_idx,
            "status": self.status.value,
            "assigned_robot": self.assigned_robot,
            "subtasks": [st.to_dict() for st in self.subtasks],
        }


class TaskManager:
    """작업 관리자"""

    def __init__(self, shelf_manager, path_planner):
        self.shelf_manager = shelf_manager
        self.path_planner = path_planner
        self.tasks: Dict[str, PickingTask] = {}     # task_id -> PickingTask
        # shelf_id -> [(task_id, workstation_id, items)] 대기 목록
        self.shelf_demand: Dict[int, List[Dict]] = {}

    def create_task(self, task_id: str, workstation_id: int, items: List[str]) -> Optional[PickingTask]:
        """
        피킹 작업 생성 및 서브태스크 분해

        1. 필요 물품 → 선반 매핑
        2. 선반 방문 순서 결정 (가까운 순)
        3. 각 선반에 대해 서브태스크 시퀀스 생성
        """
        # 물품 → 선반 매핑
        shelf_items = self.shelf_manager.find_shelves_for_items(items)
        if not shelf_items:
            print(f"[TaskManager] Task {task_id}: no shelves found for items {items}")
            return None

        # 선반 방문 순서: 작업대에서 가까운 순
        shelf_ids = list(shelf_items.keys())
        shelf_ids.sort(key=lambda sid: self.path_planner._heuristic(workstation_id, sid))

        # 서브태스크 생성
        subtasks: List[SubTask] = []
        subtask_counter = 0

        for shelf_id in shelf_ids:
            items_on_shelf = shelf_items[shelf_id]

            # 1) 선반으로 이동
            subtask_counter += 1
            subtasks.append(SubTask(
                subtask_id=f"{task_id}_ST{subtask_counter}",
                subtask_type=SubTaskType.GO_TO_SHELF,
                shelf_id=shelf_id,
                target_node=shelf_id,
            ))

            # 2) 선반 들어올리기
            subtask_counter += 1
            subtasks.append(SubTask(
                subtask_id=f"{task_id}_ST{subtask_counter}",
                subtask_type=SubTaskType.PICKUP_SHELF,
                shelf_id=shelf_id,
                target_node=shelf_id,
            ))

            # 3) 작업대로 배달
            subtask_counter += 1
            subtasks.append(SubTask(
                subtask_id=f"{task_id}_ST{subtask_counter}",
                subtask_type=SubTaskType.DELIVER_TO_WS,
                shelf_id=shelf_id,
                target_node=workstation_id,
            ))

            # 4) 작업자 픽업 대기
            subtask_counter += 1
            subtasks.append(SubTask(
                subtask_id=f"{task_id}_ST{subtask_counter}",
                subtask_type=SubTaskType.WAIT_PICKING,
                shelf_id=shelf_id,
                target_node=workstation_id,
                items_to_pick=items_on_shelf,
            ))

            # 5) 선반 복귀 (실행 시 포워딩으로 변경될 수 있음)
            subtask_counter += 1
            subtasks.append(SubTask(
                subtask_id=f"{task_id}_ST{subtask_counter}",
                subtask_type=SubTaskType.RETURN_SHELF,
                shelf_id=shelf_id,
                target_node=shelf_id,  # 기본값: 원래 위치, 실행 시 갱신됨
            ))

        task = PickingTask(
            task_id=task_id,
            workstation_id=workstation_id,
            items=items,
            shelf_sequence=shelf_ids,
            subtasks=subtasks,
        )
        self.tasks[task_id] = task

        # 선반 수요 등록
        for shelf_id, items_on_shelf in shelf_items.items():
            if shelf_id not in self.shelf_demand:
                self.shelf_demand[shelf_id] = []
            self.shelf_demand[shelf_id].append({
                "task_id": task_id,
                "workstation_id": workstation_id,
                "items": items_on_shelf,
            })

        print(f"[TaskManager] Task {task_id} created: {len(items)} items, "
              f"{len(shelf_ids)} shelves, {len(subtasks)} subtasks")
        return task

    def create_batch_tasks(self, task_list: List[Dict]) -> List[PickingTask]:
        """
        작업 일괄 등록
        Input: [{"task_id": "T1", "workstation_id": 50, "items": ["A","B"]}, ...]
        """
        created = []
        for task_data in task_list:
            task = self.create_task(
                task_id=task_data["task_id"],
                workstation_id=task_data["workstation_id"],
                items=task_data["items"],
            )
            if task:
                created.append(task)
        return created

    def get_next_pending_task(self) -> Optional[PickingTask]:
        """대기 중인 다음 작업"""
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                return task
        return None

    def start_task(self, task_id: str, robot_id: int) -> Optional[SubTask]:
        """작업 시작 → 첫 서브태스크 반환"""
        task = self.tasks.get(task_id)
        if not task:
            return None

        task.status = TaskStatus.IN_PROGRESS
        task.assigned_robot = robot_id

        first = task.get_current_subtask()
        if first:
            first.status = TaskStatus.IN_PROGRESS
            first.assigned_robot = robot_id
        return first

    def handle_item_picked(self, task_id: str, item: str) -> Dict[str, Any]:
        """
        작업자가 물품 픽업 완료 신호를 보냈을 때 처리

        Returns:
            {"action": "continue_picking", "remaining": [...]}
            또는
            {"action": "shelf_done", "next_action": "return"/"forward", ...}
        """
        task = self.tasks.get(task_id)
        if not task:
            return {"action": "error", "message": f"Task {task_id} not found"}

        # 물품 픽업 기록
        if item not in task.items_picked:
            task.items_picked.append(item)

        # 현재 서브태스크 (WAIT_PICKING이어야 함)
        current_st = task.get_current_subtask()
        if not current_st or current_st.subtask_type != SubTaskType.WAIT_PICKING:
            return {"action": "error", "message": "Not in WAIT_PICKING state"}

        # 이 선반에서 아직 가져올 물품이 남았는지 확인
        remaining = [i for i in current_st.items_to_pick if i not in task.items_picked]

        if remaining:
            return {
                "action": "continue_picking",
                "remaining_items_on_shelf": remaining,
                "total_remaining": [i for i in task.items if i not in task.items_picked],
            }

        # 이 선반의 모든 물품 픽업 완료 → 다음 단계 결정
        return self._decide_shelf_action(task, current_st.shelf_id)

    def _decide_shelf_action(self, task: PickingTask, shelf_id: int) -> Dict[str, Any]:
        """
        선반의 물품 픽업이 끝난 후 다음 행동 결정:
        - 다른 작업대에서도 이 선반이 필요하면 → forward
        - 아니면 → return (가장 가까운 빈 자리로)
        """
        forward_ws = self._check_shelf_needed_elsewhere(shelf_id, task.workstation_id)

        # WAIT_PICKING 완료 → 다음 서브태스크 (RETURN_SHELF)로 진행
        next_st = task.advance_subtask()

        if forward_ws is not None and next_st:
            # 포워딩: RETURN_SHELF → FORWARD_SHELF로 변경
            next_st.subtask_type = SubTaskType.FORWARD_SHELF
            next_st.target_node = forward_ws
            return {
                "action": "shelf_done",
                "next_action": "forward",
                "forward_to_ws": forward_ws,
                "shelf_id": shelf_id,
            }
        elif next_st:
            # 복귀: 가장 가까운 빈 선반 자리로
            empty_pos = self.shelf_manager.find_nearest_empty_position(
                task.workstation_id, self.path_planner
            )
            if empty_pos is None:
                empty_pos = shelf_id  # fallback: 원래 위치
            next_st.target_node = empty_pos
            return {
                "action": "shelf_done",
                "next_action": "return",
                "return_to": empty_pos,
                "shelf_id": shelf_id,
            }

        return {"action": "error", "message": "No next subtask available"}

    def handle_subtask_complete(self, task_id: str) -> Dict[str, Any]:
        """
        서브태스크 완료 처리 (로봇 도착 등)
        다음 서브태스크를 반환하거나 작업 완료 처리
        """
        task = self.tasks.get(task_id)
        if not task:
            return {"action": "error", "message": f"Task {task_id} not found"}

        current_st = task.get_current_subtask()
        if not current_st:
            return {"action": "error", "message": "No current subtask"}

        # WAIT_PICKING은 handle_item_picked에서 처리
        if current_st.subtask_type == SubTaskType.WAIT_PICKING:
            return {
                "action": "wait_picking",
                "items_to_pick": current_st.items_to_pick,
                "shelf_id": current_st.shelf_id,
            }

        # 다음 서브태스크로 진행
        next_st = task.advance_subtask()

        if next_st is None:
            # 모든 서브태스크 완료
            task.status = TaskStatus.COMPLETED
            self._remove_shelf_demand(task_id)
            return {
                "action": "task_complete",
                "task_id": task_id,
                "items_picked": task.items_picked,
            }

        return {
            "action": "next_subtask",
            "subtask": next_st.to_dict(),
        }

    def _check_shelf_needed_elsewhere(self, shelf_id: int, current_ws: int) -> Optional[int]:
        """
        이 선반이 다른 작업대에서도 필요한지 확인
        Returns: 다른 작업대 노드 ID, 또는 None
        """
        demands = self.shelf_demand.get(shelf_id, [])
        for demand in demands:
            if demand["workstation_id"] != current_ws:
                # 해당 작업이 아직 진행 중인지 확인
                other_task = self.tasks.get(demand["task_id"])
                if other_task and other_task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                    # 해당 작업에서 이 선반의 물품이 아직 필요한지 확인
                    items_still_needed = [
                        i for i in demand["items"]
                        if i not in other_task.items_picked
                    ]
                    if items_still_needed:
                        return demand["workstation_id"]
        return None

    def _remove_shelf_demand(self, task_id: str) -> None:
        """완료된 작업의 선반 수요 제거"""
        for shelf_id in list(self.shelf_demand.keys()):
            self.shelf_demand[shelf_id] = [
                d for d in self.shelf_demand[shelf_id]
                if d["task_id"] != task_id
            ]
            if not self.shelf_demand[shelf_id]:
                del self.shelf_demand[shelf_id]

    def get_task(self, task_id: str) -> Optional[PickingTask]:
        """작업 조회"""
        return self.tasks.get(task_id)

    def get_all_tasks(self) -> List[PickingTask]:
        """모든 작업 조회"""
        return list(self.tasks.values())

    def get_status_summary(self) -> Dict[str, Any]:
        """전체 작업 상태 요약"""
        status_counts = {}
        for t in self.tasks.values():
            status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1

        return {
            "total_tasks": len(self.tasks),
            "status_counts": status_counts,
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }
