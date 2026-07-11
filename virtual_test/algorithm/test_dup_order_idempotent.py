"""
약점 2 회귀 테스트 — 중복 start_order 멱등성.

시나리오 (dup_order_shelf_loss_diagnosis):
    로봇이 어떤 task를 RETURN_SHELF 단계까지 수행 중(선반 든 채 복귀)인데,
    사용자 재선택 등으로 같은 주문의 start_order가 다시 발행 →
    `create_task(같은 task_id)`가 재호출됨.

회귀 핵심:
    진행 중(IN_PROGRESS) task는 **덮어써지면 안 된다**. 덮어쓰면 새 PickingTask가
    current_subtask_idx=0(GO_TO_SHELF)으로 리셋 → 복귀 중 로봇이 이미 든 선반에
    lift_up → 선반 분실. create_task는 IN_PROGRESS task에 대해 멱등이어야 한다.
"""

import pytest

from server.managers.task import SubTaskType, TaskStatus


def _pick_shelf_and_item(shelf_manager):
    """아이템이 있는 실제 선반 노드 1개와 그 위 아이템 1개를 반환."""
    for sid in shelf_manager.all_shelf_nodes:
        shelf = shelf_manager.get_shelf(sid)
        if shelf and shelf.items:
            return sid, next(iter(shelf.items))
    raise RuntimeError("아이템이 있는 선반을 찾지 못함 (shelf_config 확인)")


def test_duplicate_start_order_does_not_reset_inflight_task(handler):
    tm = handler.task_manager
    shelf_id, item = _pick_shelf_and_item(handler.shelf_manager)

    # 1) task 생성 + 시작(IN_PROGRESS) → RETURN_SHELF 단계로 점프 (복귀 중 모사)
    task = tm.create_task(
        task_id="T_dup", workstation_id=33, items=[item],
        optimized_shelf_sequence=[shelf_id],
    )
    assert task is not None
    tm.start_task("T_dup", robot_id=1)
    assert task.status == TaskStatus.IN_PROGRESS

    return_idx = next(
        i for i, st in enumerate(task.subtasks)
        if st.subtask_type == SubTaskType.RETURN_SHELF
    )
    task.current_subtask_idx = return_idx
    assert task.get_current_subtask().subtask_type == SubTaskType.RETURN_SHELF

    # 2) 중복 start_order → 같은 task_id로 create_task 재호출
    task2 = tm.create_task(
        task_id="T_dup", workstation_id=33, items=[item],
        optimized_shelf_sequence=[shelf_id],
    )

    # 3) 멱등: 같은 객체 + 진행 상태 보존 (GO_TO_SHELF로 리셋되면 안 됨)
    assert task2 is task, "진행 중 task가 새 객체로 덮어써짐 (멱등 위반 → 선반 분실 버그)"
    assert task2.current_subtask_idx == return_idx, "current_subtask_idx가 리셋됨"
    assert task2.get_current_subtask().subtask_type == SubTaskType.RETURN_SHELF


def test_pending_task_can_still_be_recreated(handler):
    """아직 시작 안 한(PENDING) task는 멱등 가드에 막히지 않고 재생성 허용."""
    tm = handler.task_manager
    shelf_id, item = _pick_shelf_and_item(handler.shelf_manager)

    t1 = tm.create_task(task_id="T_pend", workstation_id=33, items=[item],
                        optimized_shelf_sequence=[shelf_id])
    assert t1 is not None and t1.status == TaskStatus.PENDING

    # PENDING이면 재생성 허용 (idx=0이라 손실 없음)
    t2 = tm.create_task(task_id="T_pend", workstation_id=33, items=[item],
                        optimized_shelf_sequence=[shelf_id])
    assert t2 is not None
    assert t2.current_subtask_idx == 0
