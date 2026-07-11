"""
수정 54: 일반 교착(wait-for 사이클) 반응형 해소 회귀 테스트 (core 통합 레벨).

배경: 시공간 예약(예방형)은 노드 hop = 시간 축 + 매 plan마다 lockstep 가정이라,
비동기 실행(회전 step이 직진보다 오래)으로 드리프트가 누적되면 같은 회랑에서
마주보는 AGV들의 교착을 못 막을 수 있다. 그때 전원이 멈춰 영구 freeze →
_detect_deadlock_cycle(도구 위임) + _resolve_deadlock이 한쪽을 우회 재계획해 푼다.
(순수 사이클 찾기 단위 테스트는 test_deadlock_detector.py.)

맵(8×6): row2=9~16(W9), row1=1~8 / row6=41~48 = bypass. 모든 열에 세로 간선.
"""

import pytest


def _setup_headon(handler):
    """row2에서 정면 충돌: AGV-1@12 동향(→13), AGV-2@13 서향(→12). 둘 다 blocked.

    실제 버그 시나리오 재현: 둘 다 선반 운반 중(carrying) → A*가 IN_PLACE 선반 노드를
    통과 못 하므로 우회는 bypass 행(row1/row6)으로 강제된다.
    """
    sm = handler.shelf_manager
    r1 = handler.robot_manager.get_robot(1)
    r1.current_node = 12
    r1.heading = 90               # 동 → 다음 노드 13
    r1.heading_initialized = True
    r1.planned_path = [12, 13, 14, 15, 23]
    r1.command_queue = ["forward"]
    r1.carrying_shelf = 23
    sm.mark_shelf_picked_up(23, 1)

    r2 = handler.robot_manager.get_robot(2)
    r2.current_node = 13
    r2.heading = 270              # 서 → 다음 노드 12
    r2.heading_initialized = True
    r2.planned_path = [13, 12, 11, 10, 9]
    r2.command_queue = ["forward"]
    r2.carrying_shelf = 30
    sm.mark_shelf_picked_up(30, 2)

    # in_flight None (기본값) → 둘 다 _is_blocked True
    return r1, r2


def test_detect_head_on_is_2cycle(handler):
    """2대 head-on = 길이 2 wait-for 사이클로 감지된다."""
    _setup_headon(handler)
    cycle = handler._detect_deadlock_cycle()
    assert cycle is not None, "교착 사이클을 감지해야 함"
    assert set(cycle) == {1, 2}


def test_staging_robot_included_in_deadlock_cycle(handler):
    """통행권 수정: 줄 서서 기다리는 staging 로봇도 wait-for 사이클에 잡힌다.

    옛 구조적 결함: staging 로봇은 command_queue가 비어 감지기가 못 봐서
    '작업대 입구 교착'(점유자를 대기자가 막음)이 영영 안 풀렸다.
    이제 staging 로봇 → 목표 회랑 점유자 엣지를 추가해 사이클이 완성된다.
    재현: AGV-1@12(→13 막힘, W9 회랑 점유) ↔ AGV-2@13(W9 줄서기로 13에 주차).
    """
    r1 = handler.robot_manager.get_robot(1)
    r1.current_node = 12
    r1.heading = 90                    # 동 → 다음 13
    r1.heading_initialized = True
    r1.planned_path = [12, 13, 11, 10, 9]
    r1.command_queue = ["forward"]

    r2 = handler.robot_manager.get_robot(2)
    r2.current_node = 13
    r2.heading_initialized = True
    r2.command_queue = []              # staging hold = 빈 큐 (옛 감지기가 놓치던 지점)

    # AGV-2를 W9 줄서기로 등록(13에 주차), W9 회랑은 AGV-1이 점유 중
    handler.staging_manager.add_staged_agv(ws_node=9, rid=2, staging_node=13)
    handler.reservation.reserve_indefinite(1, 9)

    cycle = handler._detect_deadlock_cycle()
    assert cycle is not None, "staging 로봇이 낀 입구 교착도 감지해야 함"
    assert set(cycle) == {1, 2}


def test_staging_deadlock_resolves(handler):
    """통행권 수정 끝단 증명: staging 낀 입구 교착이 감지→자동 해소(우회)된다.

    손으로 재현하기 어려운 타이밍 버그를, 그 상태를 재구성해 결정론으로 검증.
    움직이는 AGV-1이 staging 중인 AGV-2를 피해 우회 경로를 받으면 freeze가 풀린다.
    (staging AGV-2는 planned_path가 없어 양보자가 못 됨 → 움직이는 쪽이 우회.)
    """
    # AGV-1: W9 회랑 점유 중이며 16 쪽으로 빠져나가는 길에 13을 지나야 함 (13에 AGV-2 주차)
    r1 = handler.robot_manager.get_robot(1)
    r1.current_node = 12
    r1.heading = 90                    # 동 → 다음 13
    r1.heading_initialized = True
    r1.planned_path = [12, 13, 14, 15, 16]
    r1.command_queue = ["forward"]

    # AGV-2: W9 줄서기로 13에 주차 (빈 큐), W9 회랑은 AGV-1이 점유
    r2 = handler.robot_manager.get_robot(2)
    r2.current_node = 13
    r2.heading_initialized = True
    r2.command_queue = []
    handler.staging_manager.add_staged_agv(ws_node=9, rid=2, staging_node=13)
    handler.reservation.reserve_indefinite(1, 9)

    cycle = handler._detect_deadlock_cycle()
    assert cycle is not None and set(cycle) == {1, 2}

    assert handler._resolve_deadlock(cycle) is True, "우회 경로를 찾아 풀어야 함"
    # 양보자 = 움직이는 AGV-1. 막던 staging 로봇 노드(13)를 피하고 목적지(16)는 유지.
    assert r1.planned_path, "양보자 새 경로가 나와야 함"
    assert r1.planned_path[-1] == 16, "목적지(16) 유지"
    assert 13 not in r1.planned_path, \
        f"막던 staging 로봇 노드(13)를 피해야 함: {r1.planned_path}"


def test_no_false_positive_when_not_mutual(handler):
    """한쪽만 막힌(상대가 마주보지 않음) 경우는 사이클 미형성 → None."""
    r1, r2 = _setup_headon(handler)
    # AGV-2를 옆 행으로 치워 상호성 깨기 (AGV-1의 다음 13이 비게 됨)
    r2.current_node = 21
    r2.heading = 0
    r2.planned_path = [21, 29]
    r2.command_queue = ["forward"]
    cycle = handler._detect_deadlock_cycle()
    assert cycle is None, f"상호 교착 아니면 감지하면 안 됨: {cycle}"


def test_resolve_breaks_cycle(handler):
    """해소 후 양보자가 contested 노드를 피해 우회 경로를 받아 사이클이 끊긴다."""
    r1, r2 = _setup_headon(handler)
    cycle = handler._detect_deadlock_cycle()
    assert handler._resolve_deadlock(cycle) is True, "우회 경로를 찾아야 함"

    # 양보자 = 낮은 rid = AGV-1. contested(13)를 피하고 목적지(23) 유지.
    assert r1.planned_path, "양보자 새 경로가 나와야 함"
    assert r1.planned_path[-1] == 23, "양보자 목적지(23)는 유지"
    assert 13 not in r1.planned_path, \
        f"양보자가 contested 노드(13)를 피해야 함: {r1.planned_path}"
    # 승자(AGV-2) 경로는 안 건드림
    assert r2.planned_path == [13, 12, 11, 10, 9], "승자 경로는 그대로"


def test_resolve_uses_bypass_row(handler):
    """운반 중 우회 경로는 bypass 행(row1 1~8 또는 row6 41~48)을 경유한다."""
    _setup_headon(handler)
    cycle = handler._detect_deadlock_cycle()
    assert handler._resolve_deadlock(cycle) is True

    r1 = handler.robot_manager.get_robot(1)
    bypass = set(range(1, 9)) | set(range(41, 49))
    assert bypass & set(r1.planned_path), \
        f"우회는 bypass 행을 경유해야 함: {r1.planned_path}"


def test_yielder_gets_dispatched(handler, mock_mqtt):
    """해소 시 양보자에게 첫 명령이 실제로 발행된다."""
    _setup_headon(handler)
    mock_mqtt.reset()
    cycle = handler._detect_deadlock_cycle()
    handler._resolve_deadlock(cycle)
    assert mock_mqtt.cmds_for(1), "양보자(AGV-1)에게 명령이 발행돼야 함"


def test_try_dispatch_all_resolves_freeze(handler):
    """_try_dispatch_all 진입점에서 freeze가 자동 해소된다 (매 주행 통합)."""
    r1, r2 = _setup_headon(handler)
    handler._try_dispatch_all()
    # 양보자(AGV-1)가 contested(13)를 피한 새 경로를 받음
    assert 13 not in r1.planned_path, \
        f"_try_dispatch_all이 교착을 풀어야 함: {r1.planned_path}"


def _inject_robot(handler, rid, node):
    from server.managers.robot import Robot
    from server.planning.command_queue import CommandQueue
    handler.robot_manager.robots[rid] = Robot(
        rid=rid, name=f"AGV-{rid}", home_node=node, current_node=node)
    handler.command_queues[rid] = CommandQueue(rid)
    return handler.robot_manager.robots[rid]


def test_detect_4robot_rotational_cycle(handler):
    """4대 회전 교착(A→B→C→D→A)을 사이클로 감지한다 (일반화 핵심 검증).

    격자엔 삼각형이 없어 최소 비자명 사이클은 2×2 사각 루프(길이 4). 노드
    11-12-20-19 사각형을 시계 방향으로 도는 4대가 각자 앞을 막아 회전 교착.
      11→12→20→19→11 (각 간선 존재)
    """
    cfg = [(1, 11, 90),   # @11 동 →12
           (2, 12, 180),  # @12 남 →20
           (3, 20, 270),  # @20 서 →19
           (4, 19, 0)]    # @19 북 →11
    for rid, node, heading in cfg:
        r = (handler.robot_manager.get_robot(rid)
             if rid in handler.robot_manager.robots
             else _inject_robot(handler, rid, node))
        r.current_node, r.heading, r.heading_initialized = node, heading, True
        r.command_queue = ["forward"]
        r.planned_path = [node]

    cycle = handler._detect_deadlock_cycle()
    assert cycle is not None, "4대 회전 교착을 감지해야 함"
    assert set(cycle) == {1, 2, 3, 4}, f"4대 전원이 사이클 멤버여야 함: {cycle}"


def test_chain_is_not_cycle(handler):
    """체인(머리가 빈 노드를 향함)은 교착 아님 → None (오발 방지)."""
    _inject_robot(handler, 3, 11)
    # AGV-3@11→12(AGV-1), AGV-1@12→13(AGV-2), AGV-2@13→21(빈 노드) = 체인
    r1 = handler.robot_manager.get_robot(1)
    r1.current_node, r1.heading, r1.heading_initialized = 12, 90, True
    r1.command_queue = ["forward"]
    r2 = handler.robot_manager.get_robot(2)
    r2.current_node, r2.heading, r2.heading_initialized = 13, 0, True  # →21 빈 노드
    r2.command_queue = ["forward"]
    r3 = handler.robot_manager.get_robot(3)
    r3.current_node, r3.heading, r3.heading_initialized = 11, 90, True  # →12
    r3.command_queue = ["forward"]

    assert handler._detect_deadlock_cycle() is None, "체인은 교착 아님"
