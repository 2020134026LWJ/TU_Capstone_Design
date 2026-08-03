"""
회랑 ETA 우선권 (수정 94) 회귀 테스트.

문제: 회랑 점유가 '픽업(배달 커밋)을 먼저 끝낸' 순서로 정해져, 픽업이 몇 초 빨랐어도
경로가 더 길면 정작 작업대엔 늦게 도착하면서 먼저 도착할 로봇을 세워둔다.

수정 94: `_plan_and_publish_move`가 should_stage 전에 `_corridor_eta_contender`로
같은 작업대에 **더 먼저 도착할** 로봇을 찾아 `reserve_for`로 선점권을 넘긴다.
동률/더 김이면 아무 것도 안 함(선착순 유지) → 부작용 없음.

맵(0-based): W1=32(gateway24/staging40/trigger33), W2=8(gateway16/staging0/trigger9)
"""

import pytest

from server.managers.robot import RobotStatus
from server.managers.task import PickingTask, TaskStatus


def _make_deliver_robot(handler, rid, node, ws_node, shelf_id):
    """rid 로봇을 '선반 든 채 ws_node로 배달 중' 상태로 세팅 + 태스크 등록."""
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = node
    robot.carrying_shelf = shelf_id
    robot.status = RobotStatus.DELIVERING_TO_WS
    tid = f"T_{rid}"
    robot.current_task_id = tid
    handler.task_manager.tasks[tid] = PickingTask(
        task_id=tid, workstation_id=ws_node, items=["x"], shelf_sequence=[shelf_id],
        status=TaskStatus.IN_PROGRESS, assigned_robot=rid,
    )
    return robot


@pytest.mark.stg
def test_eta_contender_picks_closer_robot(handler):
    """같은 작업대(8)로 배달 중인 두 로봇 — 더 가까운 쪽을 contender로 반환."""
    ws = 8
    # AGV-1: 노드 19(작업대8까지 더 가까움), AGV-2: 노드 27(더 멈)
    _make_deliver_robot(handler, 1, node=19, ws_node=ws, shelf_id=19)
    _make_deliver_robot(handler, 2, node=27, ws_node=ws, shelf_id=27)

    h1 = handler.path_planner._heuristic(19, ws)
    h2 = handler.path_planner._heuristic(27, ws)
    assert h1 < h2, "전제: 노드 19가 노드 27보다 작업대 8에 가깝다"

    # AGV-2 입장에서 물으면 → 더 가까운 AGV-1을 선점자로 지목
    assert handler._corridor_eta_contender(2, ws) == 1
    # AGV-1 입장에서 물으면 → 자기가 제일 가까우니 None (선착순/정상 진입)
    assert handler._corridor_eta_contender(1, ws) is None


@pytest.mark.stg
def test_eta_tie_keeps_first_come(handler):
    """ETA 동률이면 선점 없음(None) — 선착순 유지, 불필요한 뺏기 방지."""
    ws = 8
    _make_deliver_robot(handler, 1, node=19, ws_node=ws, shelf_id=19)
    _make_deliver_robot(handler, 2, node=19, ws_node=ws, shelf_id=27)  # 같은 거리
    assert handler._corridor_eta_contender(2, ws) is None


@pytest.mark.stg
def test_eta_ignores_other_workstation(handler):
    """다른 작업대(32)로 가는 로봇은 contender가 아니다."""
    _make_deliver_robot(handler, 1, node=19, ws_node=32, shelf_id=19)  # W1으로 감
    _make_deliver_robot(handler, 2, node=27, ws_node=8, shelf_id=27)   # W2로 감
    # AGV-2가 W2(8)로 가는데, AGV-1은 W1(32)행이라 무관 → None
    assert handler._corridor_eta_contender(2, 8) is None


@pytest.mark.stg
def test_eta_ignores_non_carrying_robot(handler):
    """선반을 안 든 로봇(빈 차 이동 등)은 배달 커밋 전이라 contender 아님."""
    ws = 8
    r1 = _make_deliver_robot(handler, 1, node=19, ws_node=ws, shelf_id=19)
    r1.carrying_shelf = None            # 선반 안 듦
    r1.status = RobotStatus.MOVING_TO_SHELF
    _make_deliver_robot(handler, 2, node=27, ws_node=ws, shelf_id=27)
    assert handler._corridor_eta_contender(2, ws) is None


@pytest.mark.stg
def test_reserve_for_hands_corridor_to_closer_robot(handler):
    """
    통합: 회랑이 비었을 때 늦게 커밋했지만 먼 AGV-2가 진입 시도 →
    ETA 우선권이 가까운 AGV-1에게 선점권을 넘겨 AGV-2를 스테이징시킨다.
    이후 AGV-1이 오면 '이미 인증됨'으로 그대로 진입한다.
    """
    sm = handler.staging_manager
    ws = 8
    _make_deliver_robot(handler, 1, node=19, ws_node=ws, shelf_id=19)
    _make_deliver_robot(handler, 2, node=27, ws_node=ws, shelf_id=27)

    # 수정 94 로직 재현: should_stage 전에 선점권 이전
    assert not handler.reservation.is_corridor_held(ws)
    winner = handler._corridor_eta_contender(2, ws)
    assert winner == 1
    assert sm.reserve_for(ws, winner) is True
    assert sm._owner(ws) == 1, "가까운 AGV-1이 회랑 선점"

    # AGV-2가 이제 should_stage → 점유 중으로 보여 staging_node(0) 반환
    assert sm.should_stage(ws, incoming_rid=2) == 0
    assert sm._owner(ws) == 1

    # AGV-1이 배달 계획 → 이미 owner라 '인증됨' 즉시 진입(None)
    assert sm.should_stage(ws, incoming_rid=1) is None


@pytest.mark.stg
def test_reserve_for_noop_when_already_held(handler):
    """이미 점유된 회랑엔 reserve_for가 개입하지 않는다(선착순 보존)."""
    sm = handler.staging_manager
    ws = 8
    assert sm.should_stage(ws, incoming_rid=2) is None   # AGV-2가 먼저 정상 점유
    assert sm._owner(ws) == 2
    assert sm.reserve_for(ws, 1) is False, "이미 점유 중 → 선점 거부"
    assert sm._owner(ws) == 2, "점유자 그대로"
