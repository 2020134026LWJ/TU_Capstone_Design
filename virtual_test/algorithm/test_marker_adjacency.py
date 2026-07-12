"""수정 62 — 마커 인접성 검사 (ArUco 오검출로 인한 순간이동 차단).

배경 (2026-07-12 실측): 카메라 앞에 아무것도 없는데 마커 37 → 3 → 4가 검출됐다.
수정 59의 "맵 밖 마커 무시" 필터는 ID 1~48만 통과시키는데, ArUco 오검출은 0~249에
흩어지므로 **5번 중 1번은 유효 노드로 위장해 통과한다**. 서버가 그걸 믿고 로봇을
맵 반대편으로 순간이동시켰고, heading 장부가 실제와 어긋나 엉뚱한 방향으로 회전했다.

로봇은 한 칸씩 굴러간다. 따라서 다음 마커는 '현재 노드' 아니면 '이웃'뿐이다.
"""



def test_neighbors_lookup(handler):
    """path_planner.neighbors()가 맵 edge 기준 이웃을 준다"""
    assert sorted(handler.path_planner.neighbors(4)) == [3, 5, 12]
    assert sorted(handler.path_planner.neighbors(9)) == [1, 10, 17]


def test_non_adjacent_marker_rejected(handler, helpers):
    """멀리 있는 노드의 마커는 무시된다 — 로봇 위치가 안 바뀌어야 한다"""
    robot = handler.robot_manager.get_robot(1)
    assert robot.current_node == 9          # 홈에서 시작

    # 37은 9의 이웃(1, 10, 17)이 아니다 → 물리적으로 불가능
    resp = helpers.step_marker(handler, 1, 37)

    assert resp["success"] is False
    assert resp["action"] == "non_adjacent_marker"
    assert robot.current_node == 9          # ★ 순간이동하지 않았다


def test_adjacent_marker_accepted(handler, helpers):
    """이웃 노드의 마커는 정상 수용된다 (정상 주행을 막으면 안 된다)"""
    robot = handler.robot_manager.get_robot(1)
    assert robot.current_node == 9

    helpers.step_marker(handler, 1, 17)             # 17은 9의 이웃

    assert robot.current_node == 17


def test_same_node_marker_accepted(handler, helpers):
    """현재 노드의 마커 재검출은 허용된다 (아직 안 떠난 상태)"""
    robot = handler.robot_manager.get_robot(1)

    helpers.step_marker(handler, 1, 9)              # 제자리

    assert robot.current_node == 9


def test_off_map_marker_still_rejected(handler, helpers):
    """수정 59(맵 밖 마커)도 그대로 동작해야 한다 — 인접성 검사가 그걸 가리면 안 됨"""
    robot = handler.robot_manager.get_robot(1)

    resp = helpers.step_marker(handler, 1, 145)     # 맵에 없는 ID (실측된 오검출 값)

    assert resp["success"] is False
    assert resp["action"] == "unknown_marker"
    assert robot.current_node == 9


def test_phantom_sequence_does_not_teleport(handler, helpers):
    """2026-07-12에 실제로 들어온 유령 마커 시퀀스(37 → 3 → 4)를 재현.

    수정 62 이전에는 이 셋이 전부 수용되어 로봇이 맵을 가로질러 순간이동했다.
    이제는 셋 다 9의 이웃이 아니므로 전부 거부되고 로봇은 홈에 남는다.
    """
    robot = handler.robot_manager.get_robot(1)

    for phantom in (37, 3, 4):
        helpers.step_marker(handler, 1, phantom)

    assert robot.current_node == 9          # ★ 홈에서 한 발짝도 안 움직였다


# ─── 수정 64: forward 목표 불일치 마커 거부 ──────────────────────────────

from server.planning.command_queue import CommandEntry


def _put_forward_in_flight(handler, rid, target):
    """rid에게 'forward → target' 명령이 발행된(in_flight) 상태를 만든다."""
    q = handler.command_queues[rid]
    q.enqueue(CommandEntry(cmd="forward", target_node=target))
    q.dispatch()
    return q


def test_forward_target_mismatch_rejected(handler, helpers):
    """forward 중이면 목표 노드 마커만 도착으로 인정한다.

    2026-07-12 실측 재현: 서버가 '17번으로 forward'를 명령해둔 상태에서 마커 10이
    들어왔다(마커 시트에서 9번 옆칸이 같이 잡힘). 예전엔 WARN만 찍고 그대로 믿어
    로봇을 10번으로 옮겼다 — 한 칸 순간이동.
    """
    robot = handler.robot_manager.get_robot(1)
    robot.heading_initialized = True
    assert robot.current_node == 9

    _put_forward_in_flight(handler, 1, target=17)

    # 10은 9의 이웃이라 수정 62(인접성)는 통과한다. 수정 64가 막아야 한다.
    assert 10 in handler.path_planner.neighbors(9)
    resp = helpers.step_marker(handler, 1, 10)

    assert resp["success"] is False
    assert resp["action"] == "marker_target_mismatch"
    assert robot.current_node == 9          # ★ 순간이동하지 않았다


def test_forward_target_match_accepted(handler, helpers):
    """목표 노드 마커는 정상 수용 — 정상 주행을 막으면 안 된다"""
    robot = handler.robot_manager.get_robot(1)
    robot.heading_initialized = True

    _put_forward_in_flight(handler, 1, target=17)

    helpers.step_marker(handler, 1, 17)

    assert robot.current_node == 17
