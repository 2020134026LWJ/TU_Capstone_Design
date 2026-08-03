"""
든 채 회전 × 선반 인접 비용 (수정 95) 회귀 테스트.

문제: 선반을 든 로봇이 정적선반 옆 노드에서 회전하면 '내려놓고 돌기'(lift_down+turn
+lift_up ≈ 6초)가 강제된다. 그런데 A* 경로비용은 이 페널티를 몰라서, 깨끗한 회전 노드가
같은 길이로 있어도 굳이 선반 옆에서 도는 경로를 고를 수 있었다.

수정 95: `astar_with_time(carry_turn_hazard=...)` — 든 상태에서 회전 노드가 정적선반 옆이면
비용 +carry_turn_penalty. 재료를 계획 비용에 편입해 실행시점에만 알던 걸 A*가 미리 안다.

맵(0-based): 선반 26 = row3,col2. 이웃(=회전시 선반 쓸림) = {18,25,27,34}.
"""

import pytest

from server.managers.robot import RobotStatus
from server.managers.shelf import ShelfStatus


# ─── A* 메커니즘 (path_planner 직접) ───

def test_hazard_penalty_flips_to_clean_turn(handler):
    """같은 길이의 두 경로 — 선반옆(25) 회전 vs 깨끗한(32) 회전. 페널티가 32로 뒤집는다.

    24(동쪽향)→33:
      25-경로 [24,25,33]: 25에서 회전 1번 (선반26 옆 = hazard)
      32-경로 [24,32,33]: 회전 2번이지만 전부 깨끗
    비용없으면 회전 적은 25-경로 승 → 페널티(+3)가 붙으면 32-경로 승.
    """
    pp = handler.path_planner
    hazard = set(pp.neighbors(26))            # {18,25,27,34}
    assert 25 in hazard

    before = pp.astar_with_time(start=24, goal=33, start_heading=90,
                                carry_turn_hazard=None)
    after = pp.astar_with_time(start=24, goal=33, start_heading=90,
                               carry_turn_hazard=hazard)
    np_before = pp.compress_to_node_path(before)
    np_after = pp.compress_to_node_path(after)

    assert np_before == [24, 25, 33], "재료 없으면 회전 적은 선반옆 경로"
    assert np_after == [24, 32, 33], "재료 있으면 선반옆 회전 회피 → 깨끗한 경로"
    assert 25 not in np_after


def test_hazard_penalty_still_takes_it_if_no_alternative(handler):
    """대안이 없으면(우회가 훨씬 비싸면) 페널티가 있어도 그 회전을 택한다 — 하드블록 아님."""
    pp = handler.path_planner
    # 25를 반드시 회전으로 지나야만 하는 목표를 두고, 페널티가 있어도 경로가 나오는지(막지 않음).
    hazard = {25}
    p = pp.astar_with_time(start=24, goal=25, start_heading=180, carry_turn_hazard=hazard)
    assert p is not None, "페널티는 비용일 뿐 — 경로 자체를 막으면 안 된다"


# ─── hazard 노드 집합 도출 (handler 헬퍼) ───

def test_carry_turn_hazard_nodes_from_static_shelves(handler):
    """_carry_turn_hazard_nodes() = 정적(비-CARRIED) 선반의 직교 이웃 합집합."""
    pp = handler.path_planner
    hz = handler._carry_turn_hazard_nodes()
    # 모든 IN_PLACE 선반의 이웃이 포함돼야
    for shelf in handler.shelf_manager.shelves.values():
        if shelf.status != ShelfStatus.CARRIED and shelf.current_node is not None:
            for nb in pp.neighbors(shelf.current_node):
                assert nb in hz, f"선반{shelf.current_node} 이웃 {nb} 누락"


def test_carried_shelf_excluded_from_hazard(handler):
    """어떤 선반을 들어올리면(CARRIED) 그 선반은 hazard에서 빠진다(내 선반은 위험물 아님)."""
    # 임의 선반 하나를 CARRIED로
    sid = next(iter(handler.shelf_manager.shelves))
    shelf = handler.shelf_manager.shelves[sid]
    home = shelf.current_node
    shelf.status = ShelfStatus.CARRIED

    hz = handler._carry_turn_hazard_nodes()
    # 이 선반 '단독' 이웃(다른 정적선반의 이웃이 아닌 노드)은 hazard에서 빠져야
    others = set()
    for s in handler.shelf_manager.shelves.values():
        if s is shelf or s.status == ShelfStatus.CARRIED or s.current_node is None:
            continue
        others |= set(handler.path_planner.neighbors(s.current_node))
    solo = set(handler.path_planner.neighbors(home)) - others
    for n in solo:
        assert n not in hz, f"CARRIED 선반의 단독 이웃 {n}이 아직 hazard에 있음"
