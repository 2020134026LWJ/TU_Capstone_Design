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
from typing import Dict, List, Optional, Any, Set


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
    workstation_id: int                     # 요청 작업대 노드 (33=W1, 34=W2)
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
        """다음 서브태스크로 진행 (COMPLETED로 미리 표시된 서브태스크 자동 스킵)"""
        if self.current_subtask_idx < len(self.subtasks):
            self.subtasks[self.current_subtask_idx].status = TaskStatus.COMPLETED
        self.current_subtask_idx += 1
        # 포워딩 스킵으로 미리 COMPLETED된 서브태스크 건너뜀
        while (self.current_subtask_idx < len(self.subtasks) and
               self.subtasks[self.current_subtask_idx].status == TaskStatus.COMPLETED):
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

    def create_task(
        self,
        task_id: str,
        workstation_id: int,
        items: List[str],
        optimized_shelf_sequence: Optional[List[int]] = None,
    ) -> Optional[PickingTask]:
        """
        피킹 작업 생성 및 서브태스크 분해

        1. 필요 물품 → 선반 매핑
        2. 선반 방문 순서 결정 (optimized_shelf_sequence가 있으면 사용, 없으면 거리순)
        3. 각 선반에 대해 서브태스크 시퀀스 생성
        """
        # 멱등 가드 (약점 2 + 수정 57): 같은 task_id가 이미 처리 중/완료면 덮어쓰지 않음.
        # 사용자 재선택 등으로 start_order가 중복 발행돼도, task를
        # current_subtask_idx=0(GO_TO_SHELF)으로 리셋하는 것을 구조적으로 차단.
        #  - IN_PROGRESS(약점 2): 복귀 중 로봇의 RETURN_SHELF가 GO_TO_SHELF로 리셋 →
        #    이미 든 선반에 lift_up → 선반 분실. dup_order_shelf_loss_diagnosis 참조.
        #  - COMPLETED(수정 57): 완료된 선반이 다시 PENDING으로 재생성 → AGV가 이미
        #    완료한 선반을 작업대로 재배달 → GUI는 완료분 생략(셀 없음 → 파란불 X) →
        #    shelf_complete 못 옴 → wait_picking 영구 정지(작업대 입구 freeze).
        # GUI 백엔드의 "resume = 완료분 생략"과 대칭. create_task 생애주기 전체 멱등.
        existing = self.tasks.get(task_id)
        if existing is not None and existing.status in (
            TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED
        ):
            print(f"[TaskManager] Task {task_id} already {existing.status.value} "
                  f"(idx={existing.current_subtask_idx}) — skip re-create (idempotent)")
            return existing

        # 물품 → 선반 매핑
        shelf_items = self.shelf_manager.find_shelves_for_items(items)
        if not shelf_items:
            print(f"[TaskManager] Task {task_id}: no shelves found for items {items}")
            return None

        # 선반 방문 순서 결정
        if optimized_shelf_sequence:
            # TaskScheduler가 최적화한 순서 사용 (유효한 선반만 필터)
            shelf_ids = [s for s in optimized_shelf_sequence if s in shelf_items]
            # 최적화 순서에 없는 선반 추가 (fallback)
            for sid in shelf_items:
                if sid not in shelf_ids:
                    shelf_ids.append(sid)
            print(f"[TaskManager] Using optimized shelf sequence: {shelf_ids}")
        else:
            # 기본: 작업대에서 가까운 순
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
        Input: [{"task_id": "T1", "workstation_id": 33, "items": ["A","B"],
                 "optimized_shelf_sequence": [11, 15, ...]}, ...]
        """
        created = []
        for task_data in task_list:
            task = self.create_task(
                task_id=task_data["task_id"],
                workstation_id=task_data["workstation_id"],
                items=task_data["items"],
                optimized_shelf_sequence=task_data.get("optimized_shelf_sequence"),
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

    def get_next_pending_task_fair(
        self,
        active_per_ws: Dict[int, int],
        exclude: Optional[Set[str]] = None,
    ) -> Optional[PickingTask]:
        """공정 배정: 활성 로봇이 적은 WS 태스크 우선, exclude 태스크 제외"""
        pending = [
            t for t in self.tasks.values()
            if t.status == TaskStatus.PENDING
            and (exclude is None or t.task_id not in exclude)
        ]
        if not pending:
            return None
        # 1순위: 활성 로봇이 적은 WS, 2순위: 먼저 생성된 태스크
        pending.sort(key=lambda t: (active_per_ws.get(t.workstation_id, 0), t.created_at))
        return pending[0]

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

    def handle_shelf_complete(self, task_id: str) -> Dict[str, Any]:
        """
        작업자가 선반의 모든 물품 픽업 완료 (선반 단위)

        Returns:
            {"action": "shelf_done", "next_action": "return"/"forward", ...}
            또는
            {"action": "shelf_done_pickup_for_return", "shelf_id": ...}
        """
        task = self.tasks.get(task_id)
        if not task:
            return {"action": "error", "message": f"Task {task_id} not found"}

        current_st = task.get_current_subtask()
        if not current_st or current_st.subtask_type != SubTaskType.WAIT_PICKING:
            return {"action": "error", "message": "Not in WAIT_PICKING state"}

        # 이 선반의 모든 items 한번에 picked 처리
        for item in current_st.items_to_pick:
            if item not in task.items_picked:
                task.items_picked.append(item)

        # 포워딩 재픽업 사이클 확인: 다음 서브태스크가 PICKUP_SHELF(같은 선반)이면
        # → _decide_shelf_action 대신 재픽업 액션 반환
        next_idx = task.current_subtask_idx + 1
        if next_idx < len(task.subtasks):
            peek_next = task.subtasks[next_idx]
            if (peek_next.subtask_type == SubTaskType.PICKUP_SHELF and
                    peek_next.shelf_id == current_st.shelf_id):
                task.advance_subtask()  # WAIT_PICKING → PICKUP_SHELF
                return {
                    "action": "shelf_done_pickup_for_return",
                    "shelf_id": current_st.shelf_id,
                }

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
            # 복귀: 선반의 홈 노드로 (홈이 점유된 경우만 가장 가까운 빈 자리)
            home_node = self.shelf_manager.get_shelf_home(shelf_id)
            if home_node and self.shelf_manager.is_position_available(home_node):
                return_node = home_node
            else:
                return_node = self.shelf_manager.find_nearest_empty_position(
                    task.workstation_id, self.path_planner
                )
                if return_node is None:
                    return_node = home_node or shelf_id
            next_st.target_node = return_node
            return {
                "action": "shelf_done",
                "next_action": "return",
                "return_to": return_node,
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

        # WAIT_PICKING은 handle_shelf_complete에서 처리
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

    def handle_shelf_forwarded(self, shelf_id: int, forwarded_to_ws: int) -> Optional[str]:
        """
        선반이 다른 작업대로 포워딩됨 → 해당 작업의 서브태스크 수정

        포워딩된 선반이 forwarded_to_ws에 있으므로,
        해당 작업의 GO_TO_SHELF 목적지를 forwarded_to_ws로 변경

        Returns: 수정된 task_id, 또는 None
        """
        demands = self.shelf_demand.get(shelf_id, [])
        for demand in demands:
            if demand["workstation_id"] != forwarded_to_ws:
                continue

            other_task = self.tasks.get(demand["task_id"])
            if not other_task or other_task.status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                continue

            # 해당 작업에서 이 선반 관련 서브태스크 찾아서 수정
            for st in other_task.subtasks:
                if st.shelf_id != shelf_id:
                    continue
                if st.status == TaskStatus.COMPLETED:
                    continue

                if st.subtask_type == SubTaskType.GO_TO_SHELF:
                    # 선반이 이미 작업대에 있으므로 목적지를 작업대로 변경
                    old_target = st.target_node
                    st.target_node = forwarded_to_ws
                    print(f"[TaskManager] Forwarded shelf {shelf_id}: "
                          f"GO_TO_SHELF target {old_target} → {forwarded_to_ws} "
                          f"(task {other_task.task_id})")

            return other_task.task_id

        return None

    def insert_forward_return_subtasks(
        self, task_id: str, shelf_id: int, dest_ws: int, items_to_pick: List[str]
    ) -> None:
        """포워딩 후 목적지 WS에서 재픽업+반납 서브태스크 삽입

        FORWARD_SHELF(현재 서브태스크) 바로 다음에 삽입:
          WAIT_PICKING(dest_ws, items)  → 목적지 WS에서 픽업 대기
          PICKUP_SHELF(dest_ws)         → 선반 다시 들어올리기
          RETURN_SHELF(home_node)       → 원래 위치로 반납 (target은 _handle_pickup_ack에서 확정)
        """
        task = self.tasks.get(task_id)
        if not task:
            return

        idx = task.current_subtask_idx  # 현재 = FORWARD_SHELF
        base_id = f"{task_id}_FWD{shelf_id}"

        new_subtasks = [
            SubTask(
                subtask_id=f"{base_id}_WAIT",
                subtask_type=SubTaskType.WAIT_PICKING,
                shelf_id=shelf_id,
                target_node=dest_ws,
                items_to_pick=items_to_pick,
            ),
            SubTask(
                subtask_id=f"{base_id}_PICKUP",
                subtask_type=SubTaskType.PICKUP_SHELF,
                shelf_id=shelf_id,
                target_node=dest_ws,  # 선반이 AT_WORKSTATION, 로봇은 이미 여기 있음
            ),
            SubTask(
                subtask_id=f"{base_id}_RETURN",
                subtask_type=SubTaskType.RETURN_SHELF,
                shelf_id=shelf_id,
                target_node=shelf_id,  # placeholder; _handle_pickup_ack에서 실제 위치로 갱신
            ),
        ]

        # FORWARD_SHELF 바로 다음에 삽입 (원래 next 서브태스크 앞)
        task.subtasks = task.subtasks[:idx + 1] + new_subtasks + task.subtasks[idx + 1:]
        print(f"[TaskManager] Task {task_id}: inserted WAIT+PICKUP+RETURN for shelf {shelf_id} "
              f"at dest WS {dest_ws} (items: {items_to_pick})")

    def skip_shelf_subtasks_for_forwarding(
        self, task_id: str, shelf_id: int
    ) -> Optional[SubTask]:
        """포워딩 로봇(T1)이 선반을 대신 처리하므로, T2의 해당 선반 서브태스크 건너뜀

        현재 인덱스 이후 shelf_id에 해당하는 모든 서브태스크를 COMPLETED로 표시하고
        current_subtask_idx를 다음 활성 서브태스크로 이동.

        Returns: 다음으로 실행할 서브태스크 (없으면 None = 작업 완료)
        """
        task = self.tasks.get(task_id)
        if not task:
            return None

        # 현재 위치 이후 shelf_id 관련 서브태스크 모두 COMPLETED 표시
        for st in task.subtasks[task.current_subtask_idx:]:
            if st.shelf_id == shelf_id:
                st.status = TaskStatus.COMPLETED

        # 현재 인덱스를 첫 번째 비완료 서브태스크로 이동
        while (task.current_subtask_idx < len(task.subtasks) and
               task.subtasks[task.current_subtask_idx].status == TaskStatus.COMPLETED):
            task.current_subtask_idx += 1

        next_st = task.get_current_subtask()
        if next_st:
            next_st.status = TaskStatus.IN_PROGRESS
            print(f"[TaskManager] Task {task_id}: skipped shelf {shelf_id} subtasks, "
                  f"next = {next_st.subtask_type.value} (node {next_st.target_node})")
        else:
            task.status = TaskStatus.COMPLETED
            self._remove_shelf_demand(task_id)
            print(f"[TaskManager] Task {task_id}: completed after skipping shelf {shelf_id}")

        return next_st

    def get_demand_items_for_ws(self, shelf_id: int, ws_id: int) -> List[str]:
        """dest WS에서 아직 필요한 선반의 물품 목록 반환"""
        demands = self.shelf_demand.get(shelf_id, [])
        for d in demands:
            if d["workstation_id"] == ws_id:
                other_task = self.tasks.get(d["task_id"])
                if other_task:
                    return [i for i in d["items"] if i not in other_task.items_picked]
        return []

    def find_task_waiting_for_shelf(self, shelf_id: int) -> Optional["PickingTask"]:
        """shelf_id를 WAIT_PICKING 중인 task 찾기 (포워딩 재픽업 케이스 포함)"""
        for task in self.tasks.values():
            if task.status != TaskStatus.IN_PROGRESS:
                continue
            current_st = task.get_current_subtask()
            if (current_st and
                    current_st.subtask_type == SubTaskType.WAIT_PICKING and
                    current_st.shelf_id == shelf_id):
                return task
        return None

    def remove_shelf_demand_for_shelf(self, task_id: str, shelf_id: int) -> None:
        """특정 작업의 특정 선반 수요 제거"""
        if shelf_id in self.shelf_demand:
            self.shelf_demand[shelf_id] = [
                d for d in self.shelf_demand[shelf_id]
                if d["task_id"] != task_id
            ]
            if not self.shelf_demand[shelf_id]:
                del self.shelf_demand[shelf_id]

    def _remove_shelf_demand(self, task_id: str) -> None:
        """완료된 작업의 선반 수요 제거"""
        for shelf_id in list(self.shelf_demand.keys()):
            self.shelf_demand[shelf_id] = [
                d for d in self.shelf_demand[shelf_id]
                if d["task_id"] != task_id
            ]
            if not self.shelf_demand[shelf_id]:
                del self.shelf_demand[shelf_id]

    def rotate_shelf_to_end(self, task_id: str) -> Optional[int]:
        """Bug A: 현재 블록된 선반 서브태스크 5개를 남은 순서 맨 뒤로 이동.
        다음 시도할 선반 ID를 반환; 더 이상 다음 선반이 없으면 None.

        선반당 서브태스크: GO, PICKUP, DELIVER, WAIT, RETURN = 5개
        """
        task = self.tasks.get(task_id)
        if not task:
            return None

        idx = task.current_subtask_idx
        SHELF_SUBTASKS = 5
        remaining = len(task.subtasks) - idx
        remaining_shelves = remaining // SHELF_SUBTASKS

        if remaining_shelves <= 1:
            return None  # 다음 선반 없음, 순서 변경 불가

        # 블록된 선반 5개 서브태스크를 남은 서브태스크 맨 뒤로 이동
        blocked_group = task.subtasks[idx:idx + SHELF_SUBTASKS]
        rest_group = task.subtasks[idx + SHELF_SUBTASKS:]
        task.subtasks = task.subtasks[:idx] + rest_group + blocked_group

        # shelf_sequence에서 현재(블록된) 선반을 남은 선반 맨 뒤로 이동
        completed_count = idx // SHELF_SUBTASKS
        if completed_count < len(task.shelf_sequence):
            remaining_seq = task.shelf_sequence[completed_count:]
            if len(remaining_seq) > 1:
                task.shelf_sequence = (
                    task.shelf_sequence[:completed_count]
                    + remaining_seq[1:]
                    + [remaining_seq[0]]
                )

        # 새 첫 번째 서브태스크 (다음 선반) 확인
        new_first = task.get_current_subtask()
        if new_first and new_first.subtask_type == SubTaskType.GO_TO_SHELF:
            return new_first.shelf_id
        return None

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
