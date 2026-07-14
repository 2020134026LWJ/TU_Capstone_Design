"""
인터셉트 (Node U) 회귀 테스트.

대상 시나리오:
    AGV가 RETURNING_SHELF 중 같은 선반에 대한 신규 주문 도착 →
    RETURN_SHELF → FORWARD_SHELF 변환 + 새 WS 경로 발행 + 이전 WS corridor 해제.

회귀 핵심 (FLOWCHART 수정 27):
    인터셉트로 release되는 이전 corridor의 `is_exiting` 플래그가
    반드시 False로 리셋되어야 한다. 잔존 시 다음 점유자가 OCCUPIED+is_exiting=True
    상태로 시작해 check_position_release 오작동 가능.
"""

import pytest

from server.managers.robot import RobotStatus
from server.managers.shelf import ShelfStatus
from server.managers.task import SubTaskType, TaskStatus


def _setup_returning_robot(handler, rid: int, shelf_id: int, src_ws: int,
                           current_node: int, task_id: str = "T_carry"):
    """AGV를 RETURNING_SHELF 상태로 셋업하고 src_ws corridor를 mark_exiting."""
    robot = handler.robot_manager.get_robot(rid)
    robot.heading = 0
    robot.heading_initialized = True
    robot.current_node = current_node
    robot.carrying_shelf = shelf_id
    robot.current_task_id = task_id
    handler.robot_manager.set_robot_status(rid, RobotStatus.RETURNING_SHELF)

    shelf = handler.shelf_manager.get_shelf(shelf_id)
    shelf.status = ShelfStatus.CARRIED
    shelf.current_node = current_node

    # src_ws에서 출발하던 task 생성 → RETURN_SHELF 단계로 점프
    item_for_shelf = next(iter(shelf.items))  # 어떤 1개 item이든 OK
    carrying_task = handler.task_manager.create_task(
        task_id=task_id,
        workstation_id=src_ws,
        items=[item_for_shelf],
    )
    assert carrying_task is not None, "carrying_task 생성 실패"
    carrying_task.status = TaskStatus.IN_PROGRESS
    carrying_task.assigned_robot = rid

    return_idx = next(
        i for i, st in enumerate(carrying_task.subtasks)
        if st.subtask_type == SubTaskType.RETURN_SHELF and st.shelf_id == shelf_id
    )
    carrying_task.current_subtask_idx = return_idx
    return_st = carrying_task.subtasks[return_idx]
    return_st.status = TaskStatus.IN_PROGRESS
    return_st.assigned_robot = rid

    # src_ws corridor를 OCCUPIED + is_exiting=True로 marking
    handler.staging_manager.mark_exiting(src_ws, rid)

    return carrying_task, return_st


@pytest.mark.intercept
def test_intercept_clears_is_exiting(handler, mock_mqtt):
    """
    회귀 방지 (FLOWCHART 수정 27):
    인터셉트로 release되는 이전 WS corridor의 is_exiting이 False로 리셋되고
    state도 FREE로 전환되어야 한다.
    """
    rid = 1
    shelf_id = 18         # 1-1
    src_ws = 8            # W2 (AGV-1 home)
    dst_ws = 32           # W1
    current_node = 16     # W2 gateway 부근, RETURN 진행 중

    _, return_st = _setup_returning_robot(handler, rid, shelf_id, src_ws, current_node)

    src_corridor = handler.staging_manager.corridors[src_ws]
    assert src_corridor.is_exiting is True
    assert handler.staging_manager._owner(src_ws) == rid

    # 새 task: 동일 선반을 다른 WS에서 요청
    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))
    new_task = handler.task_manager.create_task(
        task_id="T_new",
        workstation_id=dst_ws,
        items=[item],
    )
    assert new_task is not None
    assert new_task.shelf_sequence[0] == shelf_id

    # 인터셉트
    result = handler._try_intercept_returning_shelf(new_task)
    assert result is True, "RETURNING + 동일 선반 신규 주문이면 인터셉트 성공해야 함"

    # ─── 핵심 검증: NG 1 회귀 방지 ───
    assert src_corridor.is_exiting is False, \
        "인터셉트 시 src corridor의 is_exiting이 False로 리셋되어야 함 (FLOWCHART 수정 27)"
    assert handler.staging_manager._owner(src_ws) is None, \
        "큐가 비어있으므로 FREE로 전환되어야 함"

    # 부가 검증: 서브태스크 mutation + 로봇 상태 전환
    robot = handler.robot_manager.get_robot(rid)
    assert robot.status == RobotStatus.DELIVERING_TO_WS
    assert return_st.subtask_type == SubTaskType.FORWARD_SHELF
    assert return_st.target_node == dst_ws

    # 새 경로가 발행되었음 (cmd 1개 이상)
    assert len(mock_mqtt.cmds_for(rid)) > 0, \
        "인터셉트 후 새 WS로의 명령이 publish_cmd로 발행되어야 함"


@pytest.mark.intercept
def test_intercept_skipped_when_not_returning(handler):
    """
    가드 검증: RETURNING_SHELF가 아니면 인터셉트되지 않음.
    동일 선반을 운반 중이라도 DELIVERING_TO_WS 등의 상태에선 인터셉트 미발동.
    """
    rid = 1
    shelf_id = 18
    src_ws = 8

    _setup_returning_robot(handler, rid, shelf_id, src_ws, current_node=16)
    # 상태만 DELIVERING_TO_WS로 변경
    handler.robot_manager.set_robot_status(rid, RobotStatus.DELIVERING_TO_WS)

    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))
    new_task = handler.task_manager.create_task(
        task_id="T_new",
        workstation_id=32,
        items=[item],
    )
    assert handler._try_intercept_returning_shelf(new_task) is False
