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


# ─── 수정 89: 마커 직후(fresh) 발화 wrapper + 다운스트림 소비 ───

@pytest.mark.intercept
def test_carried_shelf_wrapper_fires_on_matching_pending(handler):
    """수정 89: 든 선반을 첫 선반으로 필요로 하는 PENDING 태스크가 있으면
    _try_intercept_for_carried_shelf가 발화(True) + 캐논 인터셉트 효과 발생."""
    rid = 1
    shelf_id = 18
    _setup_returning_robot(handler, rid, shelf_id, src_ws=8, current_node=16)

    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))
    handler.task_manager.create_task(task_id="T_new", workstation_id=32, items=[item])

    assert handler._try_intercept_for_carried_shelf(rid) is True
    robot = handler.robot_manager.get_robot(rid)
    assert robot.status == RobotStatus.DELIVERING_TO_WS
    carrying = handler.task_manager.get_task("T_carry").get_current_subtask()
    assert carrying.subtask_type == SubTaskType.FORWARD_SHELF
    assert carrying.target_node == 32


@pytest.mark.intercept
def test_carried_shelf_wrapper_skips_when_in_flight(handler):
    """수정 89 가드: in-flight 명령이 있으면(=마커 직후가 아님) 발화 안 함.
    fresh 순간에만 발화한다는 46.1 불변식을 wrapper 경로에서도 보존."""
    from server.planning.command_queue import CommandEntry

    rid = 1
    shelf_id = 18
    _setup_returning_robot(handler, rid, shelf_id, src_ws=8, current_node=16)
    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))
    handler.task_manager.create_task(task_id="T_new", workstation_id=32, items=[item])

    # in-flight 명령 주입 → stale 위험 구간
    handler.command_queues[rid].in_flight = CommandEntry(cmd="forward", target_node=17)

    assert handler._try_intercept_for_carried_shelf(rid) is False
    assert handler.robot_manager.get_robot(rid).status == RobotStatus.RETURNING_SHELF


@pytest.mark.intercept
def test_carried_shelf_wrapper_no_match_returns_false(handler):
    """든 선반을 필요로 하는 PENDING 태스크가 없으면 False (오발화 방지)."""
    rid = 1
    shelf_id = 18
    _setup_returning_robot(handler, rid, shelf_id, src_ws=8, current_node=16)

    # 다른 선반(19)에 대한 주문만 있음
    other_item = next(iter(handler.shelf_manager.get_shelf(19).items))
    handler.task_manager.create_task(task_id="T_other", workstation_id=32, items=[other_item])

    assert handler._try_intercept_for_carried_shelf(rid) is False


@pytest.mark.intercept
def test_intercept_downstream_consumes_new_task(handler):
    """수정 89 핵심: 인터셉트 발화 후 목적지 WS 도착 시, 기존 포워딩 도착 핸들러가
    새 주문 태스크를 소비(COMPLETED)해 이중배달을 막는지 end-to-end 검증.

    (옛 우려 '새 태스크가 PENDING으로 남아 재-fetch'가 실제로는 일어나지 않음을 증명)
    """
    rid = 1
    shelf_id = 18
    dst_ws = 32
    carrying_task, _ = _setup_returning_robot(handler, rid, shelf_id, src_ws=8, current_node=16)

    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))
    new_task = handler.task_manager.create_task(
        task_id="T_new", workstation_id=dst_ws, items=[item]
    )
    assert new_task.status == TaskStatus.PENDING

    # 1) 인터셉트 발화 (fresh)
    assert handler._try_intercept_for_carried_shelf(rid) is True

    # 2) 목적지 WS 도착 → FORWARD_SHELF putdown 처리 (기존 포워딩 캐논 플로우)
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = dst_ws
    forward_st = carrying_task.get_current_subtask()
    assert forward_st.subtask_type == SubTaskType.FORWARD_SHELF
    handler._handle_putdown_ack(robot, carrying_task, forward_st)

    # 3) 새 태스크가 소비됐는지 (이중배달 없음)
    assert new_task.status == TaskStatus.COMPLETED, \
        "포워딩 도착 핸들러가 새 주문 태스크를 소비해야 함 (skip_shelf_subtasks_for_forwarding)"
    # 4) 운반 태스크에 목적지 WS 픽업+반납 사이클이 삽입됐는지
    fwd_ids = [st.subtask_id for st in carrying_task.subtasks if "FWD" in st.subtask_id]
    assert fwd_ids, "T_carry에 WAIT+PICKUP+RETURN(FWD*)가 삽입돼야 함"
    # 5) 선반은 작업대에 놓였고 로봇은 더 이상 운반 중 아님
    assert robot.carrying_shelf is None


@pytest.mark.intercept
def test_intercept_stale_self_demand_still_lights_gui(handler, mock_mqtt):
    """수정 90 안전망 회귀: shelf_demand에 '이미 픽한(낡은) self-수요'가 남아있어도 같은 WS의
    새 주문 수요를 못 가리고 GUI 파란불(shelf_arrived)이 나가야 한다.

    [주의] 이 테스트는 `items_picked`를 직접 세팅해 handle_shelf_complete를 **우회** = 유령 수요를
    인위적으로 주입한다. 실제 HIL 흐름에선 수정 91이 픽 완료 시 그 수요를 지워 유령 자체가 안 생기므로
    90은 발동조차 안 한다. 이 테스트는 '그래도 유령이 어디선가 남으면 90이 막는다'는 안전망 증명이다.
    (근본 메커니즘=수정 91은 test_shelf_complete_removes_demand_at_pick가 검증.)

    T_carry(주문1): shelf 18, WS 32, item X, **이미 픽 완료**(items_picked=[X]) → 낡은 수요 잔존.
    T_new(주문2): shelf 18, WS 32, item X, PENDING → 진짜 수요.
    인터셉트 후 WS 32 도착 시 T_new가 소비되고 shelf_arrived가 발행돼야 한다.
    """
    rid = 1
    shelf_id = 18
    ws = 32   # 운반 태스크와 새 주문이 같은 WS (인터셉트 겹침 조건)

    carrying_task, _ = _setup_returning_robot(handler, rid, shelf_id, src_ws=ws, current_node=24)
    item = carrying_task.items[0]
    # 주문1은 이 선반 품목을 이미 픽함 → 수요는 만족됐지만 shelf_demand엔 아직 남아있음(RETURN 중)
    carrying_task.items_picked = [item]

    # 주문2: 같은 WS, 같은 선반, 같은 품목 (낡은 수요가 새 수요를 가리는 조건)
    new_task = handler.task_manager.create_task(
        task_id="T_new", workstation_id=ws, items=[item]
    )
    assert new_task.status == TaskStatus.PENDING

    # 인터셉트 발화
    assert handler._try_intercept_for_carried_shelf(rid) is True

    mock_mqtt.reset()   # 도착 이후의 발행만 관찰

    # WS 도착 → FORWARD_SHELF putdown 처리
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = ws
    forward_st = carrying_task.get_current_subtask()
    assert forward_st.subtask_type == SubTaskType.FORWARD_SHELF
    handler._handle_putdown_ack(robot, carrying_task, forward_st)

    # 핵심: 새 주문이 소비되고(WAIT+PICKUP+RETURN 삽입) shelf_arrived(GUI 파란불)가 발행돼야 함
    arrived = [t for t, _ in mock_mqtt.client.broadcasts if t == "warehouse/shelf/arrived"]
    assert arrived, "인터셉트 후 shelf_arrived(GUI 파란불)가 발행돼야 함 (수정 90)"
    fwd_ids = [st.subtask_id for st in carrying_task.subtasks if "FWD" in st.subtask_id]
    assert fwd_ids, "운반 태스크에 WAIT+PICKUP+RETURN(FWD*)가 삽입돼야 함"
    assert robot.status == RobotStatus.WAITING_FOR_PICK, \
        "포워딩 도착 후 피킹 대기 상태여야 함(WAIT_PICKING 진입)"


@pytest.mark.intercept
def test_shelf_complete_removes_demand_at_pick(handler):
    """수정 91 근본: 픽 완료(handle_shelf_complete) 시점에 그 태스크의 그 선반 수요가
    shelf_demand에서 제거돼야 한다(죽은 수요를 애초에 안 만듦). 수정 90은 이 위의 안전망."""
    tm = handler.task_manager
    shelf_id = 18
    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))

    task = tm.create_task(task_id="T_pick", workstation_id=32, items=[item])
    assert task is not None
    # 수요가 등록돼 있어야 함
    assert any(d["task_id"] == "T_pick" for d in tm.shelf_demand.get(shelf_id, [])), \
        "create_task가 수요를 등록해야 함"

    # WAIT_PICKING 단계로 진입시키고(서브태스크 [GO,PICKUP,DELIVER,WAIT,RETURN] → idx 3) 픽 완료
    task.status = TaskStatus.IN_PROGRESS
    wait_idx = next(i for i, st in enumerate(task.subtasks)
                    if st.subtask_type == SubTaskType.WAIT_PICKING)
    task.current_subtask_idx = wait_idx
    task.subtasks[wait_idx].status = TaskStatus.IN_PROGRESS

    tm.handle_shelf_complete("T_pick")

    # 근본 검증: 픽 완료 후 그 수요가 사라졌다
    assert not any(d["task_id"] == "T_pick" for d in tm.shelf_demand.get(shelf_id, [])), \
        "픽 완료 시 그 태스크의 그 선반 수요가 shelf_demand에서 제거돼야 함 (수정 91)"


@pytest.mark.intercept
def test_intercept_after_real_pick_no_ghost(handler):
    """수정 91 근본(충실한 end-to-end): 운반 태스크의 픽을 handle_shelf_complete로 '실제로' 통과시키면
    유령 수요가 생기지 않아, 같은 WS 새 주문 인터셉트가 낡은 수요에 안 가린다.

    test_intercept_stale_self_demand(90 안전망)와 대비 — 저건 유령을 주입하고, 이건 유령을 안 만든다.
    """
    tm = handler.task_manager
    shelf_id = 18
    ws = 32
    item = next(iter(handler.shelf_manager.get_shelf(shelf_id).items))

    # 주문1 운반 태스크: WAIT_PICKING까지 몰고 handle_shelf_complete로 '실제' 픽
    carry = tm.create_task(task_id="T_carry", workstation_id=ws, items=[item])
    carry.status = TaskStatus.IN_PROGRESS
    wait_idx = next(i for i, st in enumerate(carry.subtasks)
                    if st.subtask_type == SubTaskType.WAIT_PICKING)
    carry.current_subtask_idx = wait_idx
    carry.subtasks[wait_idx].status = TaskStatus.IN_PROGRESS
    tm.handle_shelf_complete("T_carry")   # ← 수정 91: 여기서 T_carry의 shelf 18 수요 제거

    # 유령이 안 생겼음을 직접 확인
    assert not any(d["task_id"] == "T_carry" for d in tm.shelf_demand.get(shelf_id, [])), \
        "실제 픽을 거치면 수정 91이 수요를 지워 유령이 없어야 함"

    # 주문2: 같은 WS·같은 선반·같은 품목
    new_task = tm.create_task(task_id="T_new2", workstation_id=ws, items=[item])

    # 이제 조회는 낡은 수요 없이 새 주문 수요를 바로 찾는다 (90 스킵에 의존하지 않음)
    assert tm.get_demand_items_for_ws(shelf_id, ws) == [item], \
        "유령이 없으므로 첫 매칭이 곧 새 주문의 진짜 수요여야 함 (수정 91 단독)"
