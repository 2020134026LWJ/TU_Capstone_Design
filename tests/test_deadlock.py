"""
교착 회피 회귀 테스트.

대상 메커니즘: `_resolve_deadlock` (전략 1 우회 → 전략 2 yield-node)
회귀 핵심:
  - 두 로봇이 서로의 next_node를 점유하면 _retry_blocked_robots가 deadlock 감지
  - 우선순위 동급(둘 다 carrying 없음) 시 max(rid)가 yield
  - yield 로봇의 planned_path가 block 로봇 노드를 우회하도록 재계산되어야 함
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
def test_head_on_deadlock_yield_robot_replans(handler, mock_mqtt):
    """
    정면 교착: AGV-1(11→12) ↔ AGV-2(12→11).
    우선순위 동급 → max(rid)=2가 yield.
    AGV-2의 새 planned_path는 차단 노드(11)를 우회해야 한다.
    """
    a = _ready(handler, rid=1, node=11, heading=90,
               planned_path=[11, 12, 13, 14])  # A 목표: 14
    b = _ready(handler, rid=2, node=12, heading=270,
               planned_path=[12, 11, 10, 9])   # B 목표: 9

    # 둘 다 forward 시도 → 서로의 노드 점유로 blocked
    assert handler._send_next_command(1) is False
    assert handler._send_next_command(2) is False
    assert 1 in handler._blocked_robots
    assert 2 in handler._blocked_robots

    # 두 로봇 모두 blocked → retry 시 _resolve_deadlock 발동
    handler._retry_blocked_robots()

    # B(yield)는 차단 해제되어야 함 + planned_path가 11 우회로 재계산
    assert 2 not in handler._blocked_robots, "yield 로봇은 우회 또는 yield-node로 해제"
    assert b.planned_path[0] == 12, "현재 위치에서 출발"
    assert 11 not in b.planned_path, \
        "차단된 노드(AGV-1의 current)를 우회해야 함"
    assert b.planned_path[-1] == 9, "원래 목적지 보존"
    assert len(mock_mqtt.cmds_for(2)) > 0, "yield 로봇에 첫 명령 발행"


@pytest.mark.deadlock
def test_carrying_robot_priority(handler, mock_mqtt):
    """
    carrying_shelf > non-carrying 우선순위 검증.
    AGV-1이 선반 운반 중이면 AGV-2가 yield 해야 함 (rid 무관).
    """
    a = _ready(handler, rid=1, node=11, heading=90,
               planned_path=[11, 12, 13])
    a.carrying_shelf = 19  # AGV-1이 선반 운반 중
    b = _ready(handler, rid=2, node=12, heading=270,
               planned_path=[12, 11, 10])

    handler._send_next_command(1)
    handler._send_next_command(2)
    handler._retry_blocked_robots()

    # AGV-1(carrying)은 우회 불필요, AGV-2가 yield
    assert b.planned_path[0] == 12
    assert 11 not in b.planned_path, "AGV-2가 양보 → 11 우회"
    assert a.planned_path == [11, 12, 13], "AGV-1의 경로는 보존"
