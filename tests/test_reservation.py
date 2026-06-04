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


# ─── keep_indefinite (Phase 4.5.0 — corridor 영구 점유 보존) ───

def test_release_keep_indefinite_preserves_corridor():
    """keep_indefinite=True면 cell/edge만 해제, indefinite은 유지."""
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=42)
    r.commit(rid=1, path=[10, 11])      # cell/edge도 같이
    r.release(1, keep_indefinite=True)
    assert not r.is_free(42, 0)          # corridor 점유 살아있음
    assert r.is_free(10, 0)              # cell은 해제됨


def test_recommit_preserves_own_indefinite():
    """corridor 점유 중 replan(재commit)해도 자기 indefinite은 유지 (snapshot resync 시나리오)."""
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=42)
    r.commit(rid=1, path=[10, 11])
    r.commit(rid=1, path=[11, 12])       # replan → 내부 release(keep_indefinite=True)
    assert not r.is_free(42, 0)          # 여전히 corridor 점유
    assert r.commit(rid=2, path=[41, 42]) is False  # 다른 AGV는 못 들어옴


def test_full_release_clears_indefinite_for_wakeup():
    """keep_indefinite=False(기본, 퇴출 시)면 indefinite 해제 → 다른 AGV 진입 가능 + 콜백."""
    r = ReservationService()
    seen = []
    r.on_release(lambda rid: seen.append(rid))
    r.reserve_indefinite(rid=1, node=42)
    r.release(1)                         # 퇴출
    assert r.is_free(42, 0)              # corridor FREE
    assert seen == [1]                   # on_release 발화 (wake-up 트리거)


def test_release_indefinite_node_keeps_path():
    """release_indefinite_node는 그 노드 점유만 풀고 rid의 cell/edge는 보존 (퇴출 경로)."""
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=33)
    r.commit(rid=1, path=[33, 34, 35])   # 퇴출 경로
    r.release_indefinite_node(33)
    assert r.is_free(33, 100)            # corridor 점유 풀림 (먼 시점)
    assert not r.is_free(34, 1)          # 퇴출 경로 cell은 유지


def test_reserve_indefinite_transfer_cleans_old_owner():
    """다른 rid로 점유 이전 시 이전 소유자 엔트리 정리 (corridor transfer, leak 방지)."""
    r = ReservationService()
    r.reserve_indefinite(rid=1, node=33)
    r.reserve_indefinite(rid=2, node=33)  # AGV-1 → AGV-2 이전
    assert not r.is_free(33, 0)           # 여전히 점유 (이제 2가)
    assert r.is_free(33, 0, exclude_rid=2)
    assert not r.is_free(33, 0, exclude_rid=1)  # 1은 더 이상 소유자 아님
    r.release(1)                          # 옛 소유자 release → 33 영향 없어야
    assert not r.is_free(33, 0)           # 2의 점유 유지


def test_is_corridor_held():
    """corridor 점유 질의 (Phase 4.5.2 — staging이 '회랑 비었나' 판단)."""
    r = ReservationService()
    assert not r.is_corridor_held(33)
    r.reserve_indefinite(rid=1, node=33)
    assert r.is_corridor_held(33)
    assert not r.is_corridor_held(33, exclude_rid=1)  # 자기 자신은 점유로 안 봄
    assert r.is_corridor_held(33, exclude_rid=2)       # 남이 보면 점유
    r.release_indefinite_node(33)
    assert not r.is_corridor_held(33)


def test_is_corridor_held_ignores_cells():
    """cell/edge(경로) 예약은 corridor 점유로 안 침 — is_free와 다른 점."""
    r = ReservationService()
    r.commit(rid=1, path=[33, 34])
    assert not r.is_corridor_held(33)   # cell 있어도 indefinite 아니면 회랑은 빈 것
    assert not r.is_free(33, 0)          # 대조: is_free는 cell도 막힌 것으로 봄


def test_corridor_owner():
    """corridor 주인 질의 (Phase 4.5.3)."""
    r = ReservationService()
    assert r.corridor_owner(33) is None
    r.reserve_indefinite(rid=1, node=33)
    assert r.corridor_owner(33) == 1
    r.reserve_indefinite(rid=2, node=33)   # transfer
    assert r.corridor_owner(33) == 2
    r.release_indefinite_node(33)
    assert r.corridor_owner(33) is None


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
