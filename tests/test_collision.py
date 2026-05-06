"""
cmd-based 충돌 회피 회귀 테스트.

대상 로직: `_send_next_command`, `_reserved_nodes`, `_blocked_robots`, `_retry_blocked_robots`
회귀 핵심:
  - 다른 로봇이 점유한 노드로 forward 시 → 보류 (_blocked_robots), publish 미발행
  - 다른 로봇이 예약한(_reserved_nodes) 노드로 forward 시 → 동일하게 보류
  - blocker가 이동하면 retry로 자동 해제 + forward 발행
"""

import pytest


def _ready_robot(handler, rid: int, node: int, heading: int):
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = node
    robot.heading = heading
    robot.heading_initialized = True
    robot.command_queue = []
    return robot


@pytest.mark.collision
def test_blocks_when_target_occupied(handler, mock_mqtt):
    """A가 forward로 가려는 노드를 B가 점유 중이면 A는 보류되고 publish 안 됨."""
    # 노드 11 (2.5, 3.5) → 동쪽(heading=90) 이웃은 12
    a = _ready_robot(handler, rid=1, node=11, heading=90)
    _ready_robot(handler, rid=2, node=12, heading=0)  # B가 A의 next 점유
    a.command_queue = ["forward"]

    ok = handler._send_next_command(1)

    assert ok is False
    assert 1 in handler._blocked_robots
    assert mock_mqtt.cmds_for(1) == [], "forward가 publish되면 안 됨"
    assert a.command_queue == ["forward"], "큐는 보존되어야 함 (재시도 가능)"
    assert 12 not in handler._reserved_nodes, "차단 시 예약 등록 안 함"


@pytest.mark.collision
def test_blocks_when_target_reserved_by_other(handler, mock_mqtt):
    """다른 로봇이 next_node를 _reserved_nodes로 예약했으면 같은 효과."""
    a = _ready_robot(handler, rid=1, node=11, heading=90)
    _ready_robot(handler, rid=2, node=4, heading=0)   # B는 멀리 있음
    a.command_queue = ["forward"]
    handler._reserved_nodes[12] = 2  # B가 노드 12로 이동 중 예약

    ok = handler._send_next_command(1)

    assert ok is False
    assert 1 in handler._blocked_robots
    assert mock_mqtt.cmds_for(1) == []
    # B의 예약은 변하지 않아야 함
    assert handler._reserved_nodes[12] == 2


@pytest.mark.collision
def test_resumes_after_blocker_moves(handler, mock_mqtt):
    """blocker가 떠난 후 _retry_blocked_robots가 자동으로 forward 발행."""
    a = _ready_robot(handler, rid=1, node=11, heading=90)
    b = _ready_robot(handler, rid=2, node=12, heading=0)
    a.command_queue = ["forward"]

    handler._send_next_command(1)
    assert 1 in handler._blocked_robots, "사전 조건: A는 차단 상태"

    # blocker가 이동
    b.current_node = 13

    handler._retry_blocked_robots()

    assert 1 not in handler._blocked_robots, "차단 해제되어야 함"
    assert mock_mqtt.cmds_for(1) == ["forward"], "forward가 publish되어야 함"
    assert handler._reserved_nodes.get(12) == 1, "발행과 동시에 next_node 예약 등록"
    assert a.command_queue == [], "큐에서 forward 소비됨"


@pytest.mark.collision
def test_marker_arrival_clears_reservation(handler, mock_mqtt):
    """forward 발행 → 예약 등록. 마커 도착 메시지 도달 시 예약 해제."""
    import json
    a = _ready_robot(handler, rid=1, node=11, heading=90)
    a.command_queue = ["forward"]
    handler._send_next_command(1)
    assert handler._reserved_nodes.get(12) == 1

    # AGV가 노드 12에 도착 보고
    handler.handle_message(json.dumps({
        "type": "marker_report", "rid": 1, "marker_id": 12, "heading": 90,
    }))

    assert 12 not in handler._reserved_nodes, \
        "마커 도착 시 _reserved_nodes에서 next_node 해제"
    assert a.current_node == 12
