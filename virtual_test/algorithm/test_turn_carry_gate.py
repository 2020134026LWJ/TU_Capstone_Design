"""
수정 93 회귀 — 든 채 회전 게이트 근본설계.

두 갈래:
  - footprint 이웃에 '정적 선반'(안 비킴) → 즉시 내려놓고 돌기.
  - footprint 이웃에 '살아있는 든 로봇'(언젠가 비킴) → 대기(내려놓기 안 함).
그리고 대기가 서로 물리면(회전 마주 막힘) _detect_deadlock_cycle가 잡고
_resolve_deadlock가 한쪽을 내려놓게 하는지 검증.
"""

import pytest


def _make_carried_turn(handler, rid, node, carry_shelf, turn="turn_left"):
    """rid를 node에서 carry_shelf를 든 채 turn을 다음 명령으로 세팅."""
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = node
    robot.heading = 0
    robot.heading_initialized = True
    robot.carrying_shelf = carry_shelf
    robot.command_queue = [turn, "forward"]
    q = handler.command_queues.get(rid)
    if q is not None:
        q.in_flight = None
        q.pending.clear()
    return robot


@pytest.mark.deadlock
def test_wait_when_live_carrier_adjacent(handler, mock_mqtt):
    """옆칸에 살아있는 든 로봇 → 대기(내려놓기 안 함). 수정 84의 과잉 내려놓기 제거."""
    # 33·34는 직교 인접. 둘 다 든 채, 34의 로봇이 33의 회전 footprint를 막는다.
    _make_carried_turn(handler, 1, 33, carry_shelf=30, turn="turn_left")
    _make_carried_turn(handler, 2, 34, carry_shelf=29, turn="turn_right")

    ok = handler._send_next_command(1)

    assert ok is False, "살아있는 든 로봇 옆에선 대기(False)해야 한다"
    assert "lift_down" not in mock_mqtt.cmds_for(1), \
        f"대기해야 하는데 내려놓기가 나갔다: {mock_mqtt.cmds_for(1)}"
    # 큐가 소비되지 않아야 함(다음 재시도 위해)
    assert handler.robot_manager.get_robot(1).command_queue[0] == "turn_left"


@pytest.mark.deadlock
def test_putdown_when_static_shelf_adjacent(handler, mock_mqtt):
    """옆칸에 정적 선반(IN_PLACE) → 즉시 내려놓고 돌기 (기다려도 안 비키므로)."""
    # 노드 17의 이웃 = {16,18,9,25}. 18은 선반 노드(기본 IN_PLACE).
    _make_carried_turn(handler, 1, 17, carry_shelf=30, turn="turn_left")
    # 상대 로봇은 멀리 치워 든로봇 위험 배제
    other = handler.robot_manager.get_robot(2)
    other.current_node = 0
    other.carrying_shelf = None

    ok = handler._send_next_command(1)

    assert ok is True, "정적 선반 옆에선 내려놓고 돌기가 dispatch돼야 한다"
    assert mock_mqtt.last_cmd(1) == "lift_down", \
        f"내려놓기(lift_down)가 나가야 하는데: {mock_mqtt.cmds_for(1)}"
    # 큐가 [lift_down_hold, turn, lift_up_hold]로 감싸진 뒤 lift_down_hold가 dispatch됨
    assert handler.robot_manager.get_robot(1).command_queue[:2] == ["turn_left", "lift_up_hold"]


@pytest.mark.deadlock
def test_mutual_turn_deadlock_detected_and_resolved(handler, mock_mqtt):
    """회전 마주 막힘: 든 로봇 둘이 서로의 footprint 이웃 점유 → 감지 + 내려놓기 해소.

    44·45는 row5 인접쌍 — 양쪽 다 이웃에 정적 선반이 없어(순수 '든로봇' 위험)
    둘 다 대기 → 상호 회전 교착이 실제로 성립한다. (33/34는 이웃 26 선반 때문에 부적합.)
    """
    _make_carried_turn(handler, 1, 44, carry_shelf=30, turn="turn_left")
    _make_carried_turn(handler, 2, 45, carry_shelf=29, turn="turn_right")

    # 둘 다 대기(서로 막음)
    assert handler._send_next_command(1) is False
    assert handler._send_next_command(2) is False

    # 1) 감지: wait-for 사이클에 두 로봇이 잡혀야 한다
    cycle = handler._detect_deadlock_cycle()
    assert cycle is not None, "상호 회전 교착이 감지돼야 한다"
    assert set(cycle) == {1, 2}, f"사이클 멤버가 {{1,2}}여야: {cycle}"

    # 2) 해소: 한쪽이 내려놓고 돌기로 강제돼야 한다
    mock_mqtt.reset()
    resolved = handler._resolve_deadlock(cycle)
    assert resolved is True, "교착이 해소돼야 한다"
    down_robots = [r for r in (1, 2) if "lift_down" in mock_mqtt.cmds_for(r)]
    assert len(down_robots) == 1, \
        f"정확히 한 로봇만 내려놓아야 한다(사슬 끊기): {mock_mqtt.cmds}"
    # 내려놓은 로봇의 큐는 [turn, lift_up_hold]로 남아 맨몸 회전 후 다시 듦
    m = down_robots[0]
    assert handler.robot_manager.get_robot(m).command_queue[1] == "lift_up_hold"
