"""수정 62 — 마커 인접성 검사 (ArUco 오검출로 인한 순간이동 차단).

배경 (2026-07-12 실측): 카메라 앞에 아무것도 없는데 유령 마커가 연달아 검출됐다.
수정 59의 "맵 밖 마커 무시" 필터는 맵에 있는 ID만 통과시키는데, ArUco 오검출은 0~249에
흩어지므로 **5번 중 1번은 유효 노드로 위장해 통과한다**. 서버가 그걸 믿고 로봇을
맵 반대편으로 순간이동시켰고, heading 장부가 실제와 어긋나 엉뚱한 방향으로 회전했다.

로봇은 한 칸씩 굴러간다. 따라서 다음 마커는 '현재 노드' 아니면 '이웃'뿐이다.

[번호 체계] 노드/마커 = 0~47 (0-based). AGV-1 홈 = 8(W2), 이웃 = 0, 9, 16.
"""



def test_neighbors_lookup(handler):
    """path_planner.neighbors()가 맵 edge 기준 이웃을 준다"""
    assert sorted(handler.path_planner.neighbors(3)) == [2, 4, 11]
    assert sorted(handler.path_planner.neighbors(8)) == [0, 9, 16]


def test_non_adjacent_marker_rejected(handler, helpers):
    """멀리 있는 노드의 마커는 무시된다 — 로봇 위치가 안 바뀌어야 한다"""
    robot = handler.robot_manager.get_robot(1)
    assert robot.current_node == 8          # 홈에서 시작

    # 36은 8의 이웃(0, 9, 16)이 아니다 → 물리적으로 불가능
    resp = helpers.step_marker(handler, 1, 36)

    assert resp["success"] is False
    assert resp["action"] == "non_adjacent_marker"
    assert robot.current_node == 8          # ★ 순간이동하지 않았다


def test_adjacent_marker_accepted(handler, helpers):
    """이웃 노드의 마커는 정상 수용된다 (정상 주행을 막으면 안 된다)"""
    robot = handler.robot_manager.get_robot(1)
    assert robot.current_node == 8

    helpers.step_marker(handler, 1, 16)             # 16은 8의 이웃

    assert robot.current_node == 16


def test_same_node_marker_accepted(handler, helpers):
    """현재 노드의 마커 재검출은 허용된다 (아직 안 떠난 상태)"""
    robot = handler.robot_manager.get_robot(1)

    helpers.step_marker(handler, 1, 8)              # 제자리

    assert robot.current_node == 8


def test_off_map_marker_still_rejected(handler, helpers):
    """수정 59(맵 밖 마커)도 그대로 동작해야 한다 — 인접성 검사가 그걸 가리면 안 됨"""
    robot = handler.robot_manager.get_robot(1)

    resp = helpers.step_marker(handler, 1, 145)     # 맵에 없는 ID (실측된 오검출 값)

    assert resp["success"] is False
    assert resp["action"] == "unknown_marker"
    assert robot.current_node == 8


def test_phantom_sequence_does_not_teleport(handler, helpers):
    """2026-07-12에 실제로 들어온 유령 마커 시퀀스를 재현.

    수정 62 이전에는 이 셋이 전부 수용되어 로봇이 맵을 가로질러 순간이동했다.
    이제는 셋 다 8의 이웃이 아니므로 전부 거부되고 로봇은 홈에 남는다.
    """
    robot = handler.robot_manager.get_robot(1)

    for phantom in (36, 2, 3):
        helpers.step_marker(handler, 1, phantom)

    assert robot.current_node == 8          # ★ 홈에서 한 발짝도 안 움직였다


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

    2026-07-12 실측 재현: 서버가 '16번으로 forward'를 명령해둔 상태에서 옆칸 마커 9가
    들어왔다(마커 시트 한 장에 여러 개가 인쇄돼 있어 옆칸이 같이 잡힘). 예전엔 WARN만
    찍고 그대로 믿어 로봇을 9번으로 옮겼다 — 한 칸 순간이동.
    """
    robot = handler.robot_manager.get_robot(1)
    robot.heading_initialized = True
    assert robot.current_node == 8

    _put_forward_in_flight(handler, 1, target=16)

    # 9는 8의 이웃이라 수정 62(인접성)는 통과한다. 수정 64가 막아야 한다.
    assert 9 in handler.path_planner.neighbors(8)
    resp = helpers.step_marker(handler, 1, 9)

    assert resp["success"] is False
    assert resp["action"] == "marker_target_mismatch"
    assert robot.current_node == 8          # ★ 순간이동하지 않았다


def test_forward_target_match_accepted(handler, helpers):
    """목표 노드 마커는 정상 수용 — 정상 주행을 막으면 안 된다"""
    robot = handler.robot_manager.get_robot(1)
    robot.heading_initialized = True

    _put_forward_in_flight(handler, 1, target=16)

    helpers.step_marker(handler, 1, 16)

    assert robot.current_node == 16


# ─── 수정 70: forward 명령에 목적지 동봉 ──────────────────────────────────

def test_forward_cmd_carries_target_node(handler, mock_mqtt, helpers):
    """서버는 forward 목적지를 **명령에 실어** 보낸다.

    2026-07-12 통합테스트 실측: 예전엔 `{"cmd":"forward"}`만 보내서 AGV·트윈이 각자
    heading으로 목적지를 **추측**했다. 유령 마커로 트윈 heading이 자가복구되자
    서버와 트윈이 서로 다른 목적지를 향했고 → 서버가 도착 보고를 거부 → 교착.
    서버는 이미 목적지를 안다. 알려주기만 하면 아무도 추측할 필요가 없다.
    """
    robot = handler.robot_manager.get_robot(1)
    robot.heading_initialized = True
    mock_mqtt.reset()

    handler._plan_and_publish_move(1, robot.current_node, 26)   # 홈 8 → 선반 26

    # 서버는 명령을 한 번에 하나씩 보낸다(in-flight 단일 슬롯). 첫 명령은 회전이므로
    # 그걸 완료시켜야 forward가 나온다.
    for _ in range(4):
        if mock_mqtt.forward_targets:
            break
        last = mock_mqtt.last_cmd(1)
        if last in ("turn_left", "turn_right", "turn_180"):
            helpers.step_cmd_ack(handler, 1, last)
        else:
            break

    assert mock_mqtt.forward_targets, "forward 명령이 발행되지 않았다"
    for rid, target in mock_mqtt.forward_targets:
        assert target is not None, "forward인데 target_node가 비었다 (추측하게 두면 갈린다)"
        assert target in handler.path_planner.nodes


def test_turn_cmd_has_no_target(handler, mock_mqtt, helpers):
    """turn/lift에는 목적지가 붙지 않는다 (forward 전용)"""
    robot = handler.robot_manager.get_robot(1)
    robot.heading_initialized = True
    mock_mqtt.reset()

    handler._plan_and_publish_move(1, robot.current_node, 26)
    for _ in range(4):
        last = mock_mqtt.last_cmd(1)
        if last in ("turn_left", "turn_right", "turn_180"):
            helpers.step_cmd_ack(handler, 1, last)
        else:
            break

    # forward 개수 == target을 실은 개수 (turn/lift엔 안 붙는다)
    assert len(mock_mqtt.forward_targets) == sum(
        1 for _, c in mock_mqtt.cmds if c == "forward")
