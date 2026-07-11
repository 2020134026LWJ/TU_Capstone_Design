"""
planning.deadlock_detector.find_wait_cycle 순수 단위 테스트 (수정 54).

서버 상태와 무관한 functional-graph 사이클 찾기만 검증. 격자 기하와 무관하므로
추상 그래프에서 3-사이클 등도 자유롭게 구성 가능.
"""
from server.planning.deadlock_detector import find_wait_cycle


def test_empty():
    assert find_wait_cycle({}) is None


def test_no_cycle_chain():
    # 1→2→3, 3은 아무도 안 기다림 = 체인
    assert find_wait_cycle({1: 2, 2: 3}) is None


def test_two_cycle():
    cycle = find_wait_cycle({1: 2, 2: 1})
    assert cycle is not None and set(cycle) == {1, 2}


def test_three_cycle():
    cycle = find_wait_cycle({1: 2, 2: 3, 3: 1})
    assert cycle is not None and set(cycle) == {1, 2, 3}


def test_cycle_with_entry_tail():
    # 4→1→2→3→1: 4는 사이클로 들어가는 꼬리, 순수 사이클은 {1,2,3}
    cycle = find_wait_cycle({4: 1, 1: 2, 2: 3, 3: 1})
    assert cycle is not None and set(cycle) == {1, 2, 3}
    assert 4 not in cycle, "진입 꼬리는 사이클에서 제외"


def test_self_loop_excluded_by_caller():
    # 자기 자신을 가리키는 건 호출자(core)가 occupier != rid로 거르지만,
    # 순수 함수에 들어오면 길이 1 사이클로 잡힘 (계약 명시용)
    cycle = find_wait_cycle({1: 1})
    assert cycle == [1]
