"""
B-selfguard 회귀 테스트 — 이동 중(in-flight) 재계획 보류 + fresh 시점 flush.

수정 51 클래스(이동 중 stale current_node로 계획 → 명령 한 칸 밀림 → 벽 forward 정지)를
구조적으로 막았는지 검증한다. 핵심 불변식:
  계획은 in_flight None(마커/cmd_ack 직후 = 위치 fresh)일 때만 일어난다.
"""


def _make_inflight_forward(handler, helpers, rid, start, goal, heading):
    """rid를 start에 두고 start→goal forward를 발행해 in_flight 상태로 만든다."""
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = start
    helpers.init_robot_heading(handler, rid, heading=heading)
    handler._plan_and_publish_move(rid, start, goal)
    return robot


def test_replan_deferred_while_inflight(handler, mock_mqtt, helpers):
    """in-flight forward 중 들어온 재계획은 보류되고 새 명령을 발행하지 않는다."""
    rid = 1
    # 12(row2,col5) → 4(row1,col5): 북쪽 직선 forward 1개
    _make_inflight_forward(handler, helpers, rid, start=12, goal=4, heading=0)

    q = handler.command_queues[rid]
    assert q.in_flight is not None and q.in_flight.cmd == "forward"
    n_before = len(mock_mqtt.cmds_for(rid))

    # 이동 중 재계획(preempt 흉내) — 다른 goal로
    handler._plan_and_publish_move(rid, handler.robot_manager.get_robot(rid).current_node, 8)

    # 보류 등록 + 새 명령 발행 0 (stale 위치 12에서 계획하지 않음)
    assert rid in handler._pending_replan
    assert handler._pending_replan[rid][0] == 8
    assert len(mock_mqtt.cmds_for(rid)) == n_before


def test_pending_replan_flushes_at_marker(handler, mock_mqtt, helpers):
    """마커 도착으로 in_flight 해제 → 보류 재계획이 fresh 위치에서 실행된다."""
    rid = 1
    _make_inflight_forward(handler, helpers, rid, start=12, goal=4, heading=0)
    handler._plan_and_publish_move(rid, 12, 8)
    assert rid in handler._pending_replan

    # AGV가 4 도착 보고 → ack + flush
    helpers.step_marker(handler, rid, 4, heading=0)

    robot = handler.robot_manager.get_robot(rid)
    assert rid not in handler._pending_replan          # flush됨
    assert robot.current_node == 4                      # 위치 fresh
    # 새 plan은 실제 도착 노드 4에서 출발 (옛 노드 12 중복 없음)
    assert robot.planned_path and robot.planned_path[0] == 4


def test_no_replan_when_idle_plans_immediately(handler, mock_mqtt, helpers):
    """in_flight 없으면(정지) 즉시 계획 — 보류 큐에 들어가지 않는다."""
    rid = 1
    robot = handler.robot_manager.get_robot(rid)
    robot.current_node = 13
    helpers.init_robot_heading(handler, rid, heading=0)

    handler._plan_and_publish_move(rid, 13, 5)

    assert rid not in handler._pending_replan
    assert len(mock_mqtt.cmds_for(rid)) >= 1            # 즉시 첫 명령 발행
