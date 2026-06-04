"""ReservationService 단위 테스트 (REFACTOR F Phase 2.2)."""
from server.reservation_service import ReservationService


# ─── commit / release / is_free 기본 ───

def test_initial_is_free_anywhere():
    r = ReservationService()
    assert r.is_free(10, 0)
    assert r.is_free(10, 5)


def test_commit_simple_path_reserves_cells():
    r = ReservationService()
    assert r.commit(rid=1, path=[10, 11, 12]) is True
    # 자기 자신은 통과
    assert r.is_free(10, 0, exclude_rid=1)
    # 다른 rid는 차단
    assert not r.is_free(10, 0, exclude_rid=2)
    assert not r.is_free(11, 1)
    assert not r.is_free(12, 2)
    # 경로 밖은 비어있음
    assert r.is_free(13, 3)


def test_commit_empty_path_is_noop_true():
    r = ReservationService()
    assert r.commit(rid=1, path=[]) is True


def test_release_clears_reservations():
    r = ReservationService()
    r.commit(rid=1, path=[10, 11])
    r.release(1)
    assert r.is_free(10, 0)
    assert r.is_free(11, 1)


# ─── 충돌 감지 ───

def test_commit_conflict_returns_false():
    r = ReservationService()
    r.commit(rid=1, path=[10, 11, 12])
    # rid=2가 동일 cell 예약 시도 → 충돌
    assert r.commit(rid=2, path=[11, 12, 13], start_time=1) is False


def test_failed_commit_preserves_existing_reservation():
    """충돌로 실패해도 다른 rid 예약은 그대로 — 자기 자신도 영향 X."""
    r = ReservationService()
    r.commit(rid=1, path=[10, 11, 12])  # (10,0), (11,1), (12,2)
    r.commit(rid=2, path=[20, 21])      # (20,0), (21,1)
    # rid=2가 (11,1) 충돌하는 path 재시도 → 실패
    assert r.commit(rid=2, path=[11, 12], start_time=1) is False
    # rid=2의 기존 예약은 유지되어야 함
    assert not r.is_free(20, 0)
    assert not r.is_free(21, 1)


def test_recommit_same_rid_replaces():
    """같은 rid의 재commit은 기존 예약 자동 release 후 새 path 적용."""
    r = ReservationService()
    r.commit(rid=1, path=[10, 11])
    assert r.commit(rid=1, path=[20, 21]) is True
    assert r.is_free(10, 0)  # 옛 path 해제됨
    assert not r.is_free(20, 0)


# ─── dwell 버퍼 (legacy timing 마진) ───

def test_dwell_zero_default_no_buffer():
    r = ReservationService()
    r.commit(rid=1, path=[10, 11], dwell=0)
    # (10, 0), (11, 1)만 점유. (10, 1)은 비어있음
    assert not r.is_free(10, 0)
    assert r.is_free(10, 1)
    assert not r.is_free(11, 1)


def test_dwell_one_adds_next_step():
    r = ReservationService()
    r.commit(rid=1, path=[10, 11], dwell=1)
    # 각 노드를 (t, t+1) 둘 다 점유
    assert not r.is_free(10, 0)
    assert not r.is_free(10, 1)  # dwell 효과
    assert not r.is_free(11, 1)
    assert not r.is_free(11, 2)


def test_dwell_release_clears_all_buffer_cells():
    r = ReservationService()
    r.commit(rid=1, path=[10, 11], dwell=1)
    r.release(1)
    assert r.is_free(10, 0)
    assert r.is_free(10, 1)
    assert r.is_free(11, 1)
    assert r.is_free(11, 2)


# ─── edge swap collision ───

def test_swap_collision_blocked():
    """A→B와 B→A 같은 시각 통과는 충돌 (위치 교환 = 실제 충돌)."""
    r = ReservationService()
    r.commit(rid=1, path=[10, 11])  # 10→11 at t=0
    assert r.commit(rid=2, path=[11, 10]) is False  # 11→10 at t=0 → swap 충돌


def test_following_path_no_swap_collision():
    """같은 방향 같은 edge 다른 시점 = OK."""
    r = ReservationService()
    r.commit(rid=1, path=[10, 11], start_time=0)
    assert r.commit(rid=2, path=[10, 11], start_time=2) is True


# ─── advance ───

def test_advance_clears_past_reservations():
    r = ReservationService()
    r.commit(rid=1, path=[10, 11, 12, 13])
    r.advance(rid=1, current_node=12, t=2)
    # 과거(t<2) 해제
    assert r.is_free(10, 0)
    assert r.is_free(11, 1)
    # 현재/미래 유지
    assert not r.is_free(12, 2)
    assert not r.is_free(13, 3)


def test_advance_no_op_when_no_reservations():
    r = ReservationService()
    r.advance(rid=99, current_node=10, t=0)  # 예외 안 남


# ─── indefinite (goal-lock) ───

def test_reserve_indefinite_blocks_all_times():
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=42)
    assert not r.is_free(42, 0)
    assert not r.is_free(42, 100)
    assert r.is_free(42, 0, exclude_rid=1)  # 자기 자신은 통과


def test_release_clears_indefinite():
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=42)
    r.release(1)
    assert r.is_free(42, 0)


def test_commit_blocked_by_other_indefinite():
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=42)
    assert r.commit(rid=2, path=[40, 41, 42]) is False


def test_indefinite_idempotent():
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=42)
    r.reserve_indefinite(rid=1, node=42)  # 중복 호출 OK
    r.release(1)
    assert r.is_free(42, 0)


# ─── on_release 콜백 ───

def test_on_release_fires_callbacks():
    r = ReservationService()
    seen = []
    r.on_release(lambda rid: seen.append(rid))
    r.commit(rid=1, path=[10])
    r.release(1)
    assert seen == [1]


def test_recommit_does_not_fire_release_callback():
    """commit 내부의 self.release(rid, fire_callbacks=False)는 콜백 무발화."""
    r = ReservationService()
    seen = []
    r.on_release(lambda rid: seen.append(rid))
    r.commit(rid=1, path=[10])
    r.commit(rid=1, path=[20])  # 재commit → 내부 release는 콜백 안 부름
    assert seen == []


def test_multiple_callbacks():
    r = ReservationService()
    a, b = [], []
    r.on_release(lambda rid: a.append(rid))
    r.on_release(lambda rid: b.append(rid))
    r.commit(rid=5, path=[10])
    r.release(5)
    assert a == [5] and b == [5]
