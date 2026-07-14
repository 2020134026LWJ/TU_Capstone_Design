"""
약점 3 회귀 테스트 — 서버가 lift_up/lift_down 발행 시 대상 선반(shelf_id)을 함께 전달.

시뮬은 이 shelf_id로 "그 선반을 직접" 들어 좌표 추측(빈 리프트/순간이동)을 없앤다.
서버 측 계약: _send_next_command가 lift 명령일 때 publish_cmd(rid, cmd, carrying_shelf).
"""

import pytest

from server.managers.robot import RobotStatus


def _prime(handler, rid: int):
    robot = handler.robot_manager.get_robot(rid)
    robot.heading = 0
    robot.heading_initialized = True
    q = handler.command_queues.get(rid)
    if q is not None:
        q.in_flight = None
    return robot


def test_lift_up_publishes_target_shelf_id(handler, mock_mqtt):
    rid = 1
    robot = _prime(handler, rid)
    # lift_up 발행 시점엔 carrying_shelf가 이미 목표 선반으로 세팅돼 있음(픽업 직전)
    robot.carrying_shelf = 26
    robot.command_queue = ["lift_up"]

    assert handler._send_next_command(rid) is True
    assert mock_mqtt.lift_shelf_ids[-1] == (rid, "lift_up", 26), \
        "lift_up 발행에 목표 shelf_id가 실리지 않음 (시뮬이 추측하게 됨)"


def test_lift_down_publishes_carried_shelf_id(handler, mock_mqtt):
    rid = 1
    robot = _prime(handler, rid)
    robot.carrying_shelf = 31  # 곧 놓을 선반
    robot.command_queue = ["lift_down"]

    assert handler._send_next_command(rid) is True
    assert mock_mqtt.lift_shelf_ids[-1] == (rid, "lift_down", 31)


def test_non_lift_cmd_has_no_shelf_id(handler, mock_mqtt):
    rid = 1
    robot = _prime(handler, rid)
    robot.carrying_shelf = 26
    # forward는 충돌체크가 있으니 turn으로 검증 (shelf_id 미부착)
    robot.command_queue = ["turn_left"]

    assert handler._send_next_command(rid) is True
    # turn 명령은 lift_shelf_ids에 기록되지 않음
    assert all(c != "turn_left" for _, c, _ in mock_mqtt.lift_shelf_ids)
