"""
교착 회피 회귀 테스트 (예방형).

REFACTOR F Phase 4.3/4.4/4.5.6: 반응형 교착 해제(`_resolve_deadlock` —
head-on yield / carrying-priority yield / staging yield)를 전부 제거하고
plan 시점 예방으로 대체.
  - head-on / swap: edge 예약(I2) → test_reservation.py::test_swap_collision_blocked
  - staging 교착: corridor staging_node를 모든 로봇 transit에서 제외 (4.5.6 Step 1, 아래)

"점유자가 staging_node 경유 경로 선커밋 → 대기 AGV가 거기 주차"의 시간차 교착을
staging yield가 풀어줬으나, staging_node 자체를 통과 불가로 만들면 점유자가
애초에 그 경로를 못 짜므로 교착이 발생하지 않음.
"""

import pytest


@pytest.mark.deadlock
def test_staging_node_excluded_from_transit(handler):
    """4.5.6 Step 1: corridor staging_node는 어떤 로봇의 경유(transit) 경로에도 안 들어간다.

    W1(33) staging_node=41. 42→33 최단경로 후보는 [42, 41, 33]이지만
    41이 transit에서 제외되어 [42, 34, 33] 등으로 우회해야 한다.
    """
    # AGV-2를 목적지(33)에서 치움 (goal 노드 점유 방지)
    other = handler.robot_manager.get_robot(2)
    other.current_node = 9

    robot = handler.robot_manager.get_robot(1)
    robot.current_node = 42
    robot.heading = 0
    robot.heading_initialized = True

    handler._plan_and_publish_move(1, 42, 33)

    assert robot.planned_path, "경로가 나와야 함"
    assert robot.planned_path[-1] == 33
    assert 41 not in robot.planned_path[1:-1], \
        f"staging_node 41이 경유 경로에 포함되면 안 됨: {robot.planned_path}"


@pytest.mark.deadlock
def test_staging_node_allowed_as_goal(handler):
    """staging_node가 그 로봇의 *목적지*면 제외 안 함 (대기 AGV는 거기로 가야 함)."""
    other = handler.robot_manager.get_robot(2)
    other.current_node = 9

    robot = handler.robot_manager.get_robot(1)
    robot.current_node = 42
    robot.heading = 0
    robot.heading_initialized = True

    handler._plan_and_publish_move(1, 42, 41)  # 목적지 = staging_node 41

    assert robot.planned_path, "staging_node를 목적지로 한 경로는 나와야 함"
    assert robot.planned_path[-1] == 41
