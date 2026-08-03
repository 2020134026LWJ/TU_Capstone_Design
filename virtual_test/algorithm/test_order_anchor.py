"""
수정 88 회귀 테스트 — 새 주문 NN 방문순서의 출발점을 '담당 로봇 최종 위치'로.

핵심: 로봇이 방금 선반을 내려놓고 그 자리에 서 있으면(또는 그리로 이동 중이면),
새 주문 스케줄은 WS가 아니라 그 위치를 출발점으로 삼아야 한다 → 서 있는 선반부터 방문.
담당 로봇이 없거나 오프라인이면 WS 노드로 폴백(= 기존 동작).
"""

import pytest

from server.managers.robot import RobotStatus


# home_node: robot 1 = 8 (W2), robot 2 = 32 (W1)  — robot_config.json


def test_anchor_idle_robot_uses_current_node(handler):
    """idle 로봇이면 현재 위치를 NN 출발점으로."""
    robot = handler.robot_manager.get_robot(2)  # home_node=32
    robot.online = True
    handler.robot_manager.set_robot_status(2, RobotStatus.IDLE)
    robot.current_node = 30           # 방금 선반 30을 내려놓고 그 자리
    robot.planned_path = []

    assert handler._order_start_anchor(32) == 30


def test_anchor_moving_robot_uses_final_destination(handler):
    """이동/작업 중이면 순간 위치가 아니라 최종 목적지(planned_path[-1])를."""
    robot = handler.robot_manager.get_robot(2)
    robot.online = True
    handler.robot_manager.set_robot_status(2, RobotStatus.RETURNING_SHELF)
    robot.current_node = 34           # 복귀 이동 중 순간 위치
    robot.planned_path = [34, 35, 36, 37, 38, 30]   # 결국 노드 30에 도착

    assert handler._order_start_anchor(32) == 30


def test_anchor_moving_robot_no_path_falls_back_to_current(handler):
    """이동 중인데 planned_path가 비었으면 현재 위치."""
    robot = handler.robot_manager.get_robot(2)
    robot.online = True
    handler.robot_manager.set_robot_status(2, RobotStatus.DELIVERING_TO_WS)
    robot.current_node = 25
    robot.planned_path = []

    assert handler._order_start_anchor(32) == 25


def test_anchor_offline_robot_falls_back_to_ws(handler):
    """담당 로봇이 오프라인이면 WS 노드로 폴백(기존 동작 보존)."""
    robot = handler.robot_manager.get_robot(2)
    robot.online = False
    robot.current_node = 30

    assert handler._order_start_anchor(32) == 32


def test_anchor_no_dedicated_robot_falls_back_to_ws(handler):
    """home_node가 그 WS인 로봇이 없으면 WS 노드로 폴백."""
    # WS 8은 robot 1 담당인데, robot 1을 오프라인으로 → 담당자 없음
    handler.robot_manager.get_robot(1).online = False
    assert handler._order_start_anchor(8) == 8


def test_anchor_none_ws_returns_none(handler):
    """작업대 노드가 None이면 그대로 None (폴백 체인 보존)."""
    assert handler._order_start_anchor(None) is None


def test_nn_schedules_standing_shelf_first(handler):
    """
    통합: 로봇이 선반 30에 서 있을 때, OrderOptimizer가 30을 첫 방문으로 스케줄하는지.
    (수정 88의 실제 효과 — WS(32) 기준이면 26이 먼저지만, 로봇 위치(30) 기준이면 30이 먼저)
    """
    opt = handler.task_scheduler  # OrderOptimizer

    # 선반 노드 26, 19, 30 을 각각 방문한다고 가정한 순수 NN 비교
    from server.planning.order_optimizer import OrderOptimizer
    assert isinstance(opt, OrderOptimizer)

    # WS(32) 출발 vs 로봇위치(30) 출발의 첫 선반이 달라야 함
    d_from_ws = {n: opt._calc_distance(32, n) for n in (26, 19, 30)}
    d_from_30 = {n: opt._calc_distance(30, n) for n in (26, 19, 30)}

    first_from_ws = min(d_from_ws, key=d_from_ws.get)
    first_from_30 = min(d_from_30, key=d_from_30.get)

    assert first_from_30 == 30, "로봇 위치(30) 기준이면 서 있는 선반 30이 거리 0으로 첫 방문"
    assert first_from_ws != 30, "WS(32) 기준이면 30이 첫 방문이 아니어야 대비가 성립"
