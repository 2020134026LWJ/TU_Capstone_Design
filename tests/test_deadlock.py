"""
교착 회피 회귀 테스트.

REFACTOR F Phase 4.3: 이동 중 로봇 정면 교착의 반응형 yield(`_resolve_deadlock`
전략 1 우회 / 전략 2 yield-node) 제거됨. plan 시점 edge 예약(`is_edge_free`)이
swap/정면충돌을 차단(I2)하므로 사후 yield 불필요.
  → head-on yield / carrying-priority yield 테스트 삭제 (제거된 동작 검증이었음)
  → 대체재(plan 시점 swap 차단)는 test_reservation.py::test_swap_collision_blocked 가 검증

남은 검증: staging yield (corridor 대기 AGV 비켜주기 — 4.5 전까지 유지).
"""

import pytest

from server.path_planner import PathPlanner


def _ready(handler, rid: int, node: int, heading: int, planned_path):
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = node
    robot.heading = heading
    robot.heading_initialized = True
    robot.command_queue = ["forward"]
    robot.planned_path = list(planned_path)
    robot.carrying_shelf = None
    return robot


@pytest.mark.deadlock
def test_staging_blocker_forces_yield(handler, mock_mqtt):
    """
    Staging AGV가 corridor 점유자의 경로를 막는 deadlock 시나리오 (수정 28).

    Setup:
      - AGV-1: corridor(W1=33) 점유, 노드 42에서 heading 서쪽(270)으로 forward
        → 다음 노드 41(=staging 위치)이 AGV-2 점유로 blocked
      - AGV-2: staging_wait (corridor.queue에 등록, command_queue 비어있음, 노드 41)

    Expected:
      - _try_dispatch_all()에서 staging blocker(AGV-2) 감지
      - _resolve_deadlock에서 AGV-2 강제 yield (carrying 동급이어도)
      - AGV-2가 _yielded_staging_robots에 등록
      - AGV-2에 1-step 이동 명령(turn/forward) 발행됨
    """
    sm = handler.staging_manager

    # AGV-1: corridor 점유 (W1)
    sm.should_stage(33, incoming_rid=1)  # occupying_rid=1
    # AGV-2: 큐 등록 (staging_node=41)
    sm.add_staged_agv(33, rid=2, staging_node=41)

    # AGV-1 위치/상태
    a = _ready(handler, rid=1, node=42, heading=270, planned_path=[42, 41, 33])
    a.carrying_shelf = 27  # carrying (현실적 설정)

    # AGV-2 staging_wait — command_queue 비어있음
    b = handler.robot_manager.get_robot(2)
    b.current_node = 41
    b.heading = 90
    b.heading_initialized = True
    b.command_queue = []
    b.planned_path = []
    b.carrying_shelf = 19  # carrying (동급 priority여도 staging이 양보)

    # AGV-1 forward → 41 점유로 blocked
    assert handler._send_next_command(1) is False
    assert handler._is_blocked(1)

    # AGV-2는 staging이라 blocked 아님 (command_queue 비어있음)
    assert not handler._is_blocked(2)
    assert handler._is_staging_robot(2) is True

    mock_mqtt.reset()

    # retry → staging blocker 감지 → resolve_deadlock → AGV-2 강제 yield
    handler._try_dispatch_all()

    # AGV-2가 yielded set에 등록됨
    assert 2 in handler._yielded_staging_robots, "staging AGV가 yielded set에 등록되어야 함"

    # AGV-2에 명령 발행됨 (turn 또는 forward)
    cmds_for_2 = mock_mqtt.cmds_for(2)
    assert len(cmds_for_2) > 0, "yielded AGV-2에 1-step 이동 명령 발행"

    # AGV-2의 새 planned_path는 1-step 길이 (current → yield_node)
    assert len(b.planned_path) == 2, \
        f"staging yield는 1-step (현재→yield_node), 실제: {b.planned_path}"
