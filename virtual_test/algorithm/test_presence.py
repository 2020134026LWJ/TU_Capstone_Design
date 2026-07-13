"""
수정 75 회귀 테스트 — AGV 접속/이탈(presence).

두 개념이 **다르다**는 게 이 파일의 전부다:
  online    = 지금 붙어 있나        → 태스크를 줘도 되나
  ever_seen = 한 번이라도 붙었나    → 바닥에 실재하나 (A*가 피해야 하나)

안 켠 AGV는 서버 장부에만 있는 유령이다 — 일을 시켜도 안 되고, 길을 막아서도 안 된다.
통신이 끊긴 AGV는 반대다 — 일은 못 시키지만 **몸은 그 칸에 그대로 서 있다.**
"""

import json

import pytest

from server.managers.robot import RobotStatus


def _prime(handler, rid: int, node: int, online: bool = True):
    """로봇을 '정지 + heading 확인됨' 상태로 세팅. online은 인자로 조절."""
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = node
    robot.heading = 0
    robot.heading_initialized = True
    robot.status = RobotStatus.IDLE
    robot.planned_path = []
    robot.carrying_shelf = None
    robot.online = online
    robot.ever_seen = online
    q = handler.command_queues.get(rid)
    if q is not None:
        q.in_flight = None
    return robot


# ─── 배정 게이트 (online) ───

def test_offline_robot_is_never_dispatched(handler):
    """안 켠 AGV는 유휴로 보이지만 태스크를 받으면 안 된다."""
    _prime(handler, 1, node=9, online=False)
    _prime(handler, 2, node=33, online=True)

    picked = handler.robot_manager.get_available_robot()
    assert picked is not None, "켜져 있는 AGV-2가 있는데 아무도 안 뽑혔다"
    assert picked.rid == 2, f"꺼져 있는 AGV-{picked.rid}에게 태스크가 배정됐다 (유령 로봇)"


def test_no_robot_available_when_all_offline(handler):
    """전부 꺼져 있으면 배정할 로봇이 없다 (있는 척하지 않는다)."""
    _prime(handler, 1, node=9, online=False)
    _prime(handler, 2, node=33, online=False)

    assert handler.robot_manager.get_available_robot() is None


def test_lwt_offline_stops_further_dispatch(handler):
    """주행 중 통신이 끊기면(LWT) 그 AGV에게 새 태스크가 안 나간다."""
    _prime(handler, 1, node=9, online=True)
    _prime(handler, 2, node=33, online=False)
    assert handler.robot_manager.get_available_robot().rid == 1

    handler.handle_message(json.dumps({"type": "presence", "rid": 1, "online": False}))

    assert handler.robot_manager.get_available_robot() is None, \
        "연결 끊긴 AGV-1에게 계속 태스크가 배정된다"


def test_presence_online_restores_dispatch(handler):
    """다시 켜면 배정이 재개된다 (retained birth / 재접속)."""
    _prime(handler, 1, node=9, online=False)
    _prime(handler, 2, node=33, online=False)
    assert handler.robot_manager.get_available_robot() is None

    handler.handle_message(json.dumps({"type": "presence", "rid": 1, "online": True}))

    picked = handler.robot_manager.get_available_robot()
    assert picked is not None and picked.rid == 1


def test_marker_report_marks_robot_online(handler):
    """presence를 안 쏘는 클라이언트라도, 마커를 보고했으면 붙어있는 것이다 (fallback)."""
    robot = _prime(handler, 1, node=9, online=False)
    assert robot.online is False

    handler.handle_message(json.dumps({"type": "marker_report", "rid": 1, "marker_id": 9}))

    assert robot.online is True and robot.ever_seen is True


# ─── 장애물 여부 (ever_seen) ───

def test_never_connected_robot_does_not_block_path(handler):
    """한 번도 안 켠 AGV는 바닥에 없다 → A*가 그 칸을 피할 이유가 없다."""
    _prime(handler, 1, node=2, online=True)
    _prime(handler, 2, node=3, online=False)   # 2→4 최단 경로 한가운데. 하지만 없는 로봇이다.

    handler._plan_and_publish_move(1, 2, 4)

    path = handler.robot_manager.get_robot(1).planned_path
    assert 3 in path, (
        f"존재하지도 않는 AGV-2 때문에 노드 3을 우회했다 (경로 {path}). "
        "안 켠 로봇이 길을 막으면 안 된다."
    )


def test_disconnected_robot_still_blocks_path(handler):
    """통신이 끊긴 AGV는 몸이 그 칸에 남아있다 → 계속 피해 다녀야 한다.

    이걸 틀리면 죽은 AGV에 그대로 들이받는다. presence의 가장 위험한 오답.
    """
    _prime(handler, 1, node=2, online=True)
    robot2 = _prime(handler, 2, node=3, online=True)   # 켜진 적 있다 = 바닥에 있다

    handler.handle_message(json.dumps({"type": "presence", "rid": 2, "online": False}))
    assert robot2.online is False
    assert robot2.ever_seen is True, "연결이 끊겼다고 몸이 사라지지는 않는다"

    handler._plan_and_publish_move(1, 2, 4)

    path = handler.robot_manager.get_robot(1).planned_path
    assert 3 not in path, (
        f"연결 끊긴 AGV-2가 서 있는 노드 3을 통과하는 경로를 냈다 (경로 {path}). "
        "MQTT가 끊긴 것이지 로봇이 사라진 게 아니다 — 충돌한다."
    )
