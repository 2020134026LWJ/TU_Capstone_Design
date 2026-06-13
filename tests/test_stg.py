"""
스테이징 게이팅 (STG) 회귀 테스트.

대상 메커니즘:
  - `should_stage(ws, rid)`: FREE면 None(즉시 진입), OCCUPIED면 staging_node 반환
  - `add_staged_agv(...)`: 큐에 등록
  - `mark_exiting(ws, rid)`: 퇴출 시작 — state=OCCUPIED, is_exiting=True
  - `handle_marker_trigger(rid, marker_id)`: trigger 마커 통과 → 큐 승계 + release
  - `check_position_release(rid, node)`: corridor_area 밖 이동 시 자동 해제 (인터셉트 케이스)

설정 (수정 28 기준):
  - W1=33: gateway=25, staging=41 (Row 6, 작업대 아래), trigger=34
  - W2=9:  gateway=17, staging=1  (Row 1, 작업대 위),   trigger=10
staging_node를 corridor 진입 경로 밖으로 분리 → "대기자가 입구를 막는" deadlock 제거.
"""

import pytest


# ─── 4. STG gating basic ───
# 점유(owner)는 reservation 단일 진실 (Phase 4.5.5b) → sm._owner(ws)로 질의.

@pytest.mark.stg
def test_stg_basic_first_enters_second_stages(handler):
    """빈 corridor → 첫 AGV 즉시 진입(None). 점유된 corridor → 두 번째 AGV는 staging_node 반환."""
    sm = handler.staging_manager
    ws = 33  # W1, staging_node=41

    first = sm.should_stage(ws, incoming_rid=1)
    assert first is None, "빈 corridor → 즉시 진입(None)"
    assert sm._owner(ws) == 1

    second = sm.should_stage(ws, incoming_rid=2)
    assert second == 41, "이미 점유 → AGV-2는 staging_node(41)로 우회"
    assert sm._owner(ws) == 1, "점유자는 그대로"

    sm.add_staged_agv(ws, rid=2, staging_node=second)
    assert any(s.rid == 2 for s in sm.corridors[ws].queue)


@pytest.mark.stg
def test_stg_same_rid_reentry_is_noop(handler):
    """같은 rid가 should_stage 재호출 → 이미 인증됨, None 반환."""
    sm = handler.staging_manager
    ws = 33
    assert sm.should_stage(ws, incoming_rid=1) is None
    assert sm.should_stage(ws, incoming_rid=1) is None, "동일 rid 재호출은 즉시 통과"


# ─── 5. STG release via trigger marker ───

@pytest.mark.stg
def test_release_via_trigger_marker(handler):
    """
    퇴출 AGV가 trigger 마커 통과 → handle_marker_trigger →
    release_corridor_without_trigger 위임 호출 → 큐 승계 + is_exiting 리셋.
    """
    sm = handler.staging_manager
    ws = 33                    # W1
    trigger_node = 34          # W1 trigger

    # AGV-1이 W1을 점유 + 퇴출 시작
    assert sm.should_stage(ws, incoming_rid=1) is None
    sm.mark_exiting(ws, rid=1)
    corridor = sm.corridors[ws]
    assert corridor.is_exiting is True

    # AGV-2가 staged 큐에서 대기
    assert sm.should_stage(ws, incoming_rid=2) == 41
    sm.add_staged_agv(ws, rid=2, staging_node=41)

    # AGV-1이 trigger 노드 통과
    released = sm.handle_marker_trigger(rid=1, marker_id=trigger_node)

    assert released is not None, "trigger 통과 시 큐의 다음 AGV가 release되어야 함"
    assert released.rid == 2
    assert sm._owner(ws) == 2, "다음 AGV가 점유 승계"
    assert corridor.is_exiting is False, "승계 시 is_exiting 리셋 (수정 24 위임 패턴)"
    assert len(corridor.queue) == 0


@pytest.mark.stg
def test_release_via_trigger_with_empty_queue(handler):
    """큐가 비어있을 때 trigger 통과 → corridor가 FREE로 전환 + is_exiting 리셋."""
    sm = handler.staging_manager
    ws = 9                     # W2
    trigger_node = 10          # W2 trigger

    sm.should_stage(ws, incoming_rid=1)
    sm.mark_exiting(ws, rid=1)
    assert sm.corridors[ws].is_exiting is True

    released = sm.handle_marker_trigger(rid=1, marker_id=trigger_node)

    assert released is None, "큐 비어있음 → release할 AGV 없음"
    corridor = sm.corridors[ws]
    assert sm._owner(ws) is None
    assert corridor.is_exiting is False


@pytest.mark.stg
def test_release_skip_when_rid_mismatch(handler):
    """다른 AGV가 trigger를 통과해도 점유자가 아니면 skip (방어 코딩, 수정 22 Fix B)."""
    sm = handler.staging_manager
    ws = 33
    trigger_node = 34

    sm.should_stage(ws, incoming_rid=1)
    sm.mark_exiting(ws, rid=1)

    # AGV-2가 trigger 통과 (W1 점유자가 아님) → release 거부
    released = sm.handle_marker_trigger(rid=2, marker_id=trigger_node)
    assert released is None
    assert sm._owner(ws) == 1, "점유 상태 변경 안 됨"
    assert sm.corridors[ws].is_exiting is True, "is_exiting도 그대로"


# ─── 6. STG release via position (인터셉트 등 trigger 미통과 케이스) ───

@pytest.mark.stg
def test_release_position_based_outside_corridor_area(handler):
    """
    퇴출 AGV가 trigger 노드를 통과하지 않고 corridor_area(ws_node + gateway_node) 밖으로
    이동 시 check_position_release가 corridor를 자동 해제 (인터셉트 시나리오 등).
    """
    sm = handler.staging_manager
    ws = 33                    # W1
    gateway = 25               # corridor_area = {33, 25}

    # AGV-1이 W1 점유 + 퇴출 시작
    sm.should_stage(ws, incoming_rid=1)
    sm.mark_exiting(ws, rid=1)

    # corridor_area 안에 있는 동안은 release 없음
    inside = sm.check_position_release(rid=1, node=gateway)
    assert inside is None, "gateway 안에 있으면 미해제"
    assert sm.corridors[ws].is_exiting is True

    # corridor_area 밖으로 이동 (예: 17, W2 staging — 인터셉트로 새 WS로 가는 도중)
    released = sm.check_position_release(rid=1, node=17)

    # 큐 비어있으니 released는 None, corridor는 FREE로 전환
    corridor = sm.corridors[ws]
    assert sm._owner(ws) is None
    assert corridor.is_exiting is False, "위치 기반 해제도 is_exiting 리셋"


@pytest.mark.stg
def test_position_based_release_skips_when_not_exiting(handler):
    """is_exiting=False인 corridor는 위치 기반 해제 대상 아님 (입장 진행 중 보호)."""
    sm = handler.staging_manager
    ws = 33

    sm.should_stage(ws, incoming_rid=1)
    # mark_exiting 호출 안 함 → is_exiting=False

    released = sm.check_position_release(rid=1, node=17)
    assert released is None
    # 점유 상태가 유지되어야 함 (입장 단계의 임시 위치 변경에 영향 받으면 안 됨)
    assert sm._owner(ws) == 1
