# REFACTOR F — Path/Reservation 근본 재설계

> **목표**: 사후 대응 패턴 7종을 ReservationService 단일 진실 + 명시적 SM으로 구조적 차단. "보수" 패턴 종결.
> **예상 작업량**: 7~9일 (Phase 0~8)
> **시작일**: 2026-05-27
> **브랜치**: `refactor/path-reservation-service`
> **전체 plan**: `~/.claude/plans/gleaming-enchanting-lerdorf.md`

---

## 진행 체크리스트 (세션 재진입 시 여기부터 확인)

### Phase 0 — 다이어그램 명세 보강 (반나절)
- [x] 0.1 FLOWCHART.md 백업
- [x] 0.2 PENDING 깨어남 이벤트 명시 (corridor/shelf/robot release)
- [x] 0.3 FSKIP 무한 루프 가드 명시 (`rotation_counts > N: PENDING`)
- [x] 0.4 U 노드 적용 범위 명시 (`RETURNING_SHELF`에서만)
- [x] 0.5 ROBOT_IDLE 깨어남 명시 (`TASK_WAIT → ROBOT_IDLE`)

### Phase 1 — Baseline 측정 (반나절)
- [x] 1.1 사후 대응 7종에 카운터 추가
  - [x] `_lookahead_replan` 호출 카운트
  - [x] `_resolve_deadlock` 호출 카운트
  - [x] `_find_yield_node` 호출 카운트
  - [x] `_should_hold_for_eta` true 반환 카운트
  - [x] staging redirect 발화 카운트
  - [x] goal-lock 등록 카운트
  - [x] staging cascade wake-up 카운트 (staging_manager._cascade_count)
- [x] 1.2 출력 hook 추가 (main.py stop() — Ctrl+C 시 카운터 출력)
- [x] 1.3 시연 1회 + 4 시나리오 자동 체인 → baseline 측정 (2026-06-02, 부분 측정)
- [x] 1.4 측정 결과 이 파일 하단 "Phase 1 Baseline" 섹션에 기록

### Phase 2 — ReservationService 빈 껍데기 (1일) ✅
- [x] 2.1 `server/reservation_service.py` 신규 작성 (~155줄)
  - [x] `_cells: Dict[(node, time), rid]`
  - [x] `_edges: Dict[(from, to, time), rid]`
  - [x] `_by_rid: Dict[rid, List[키]]`
  - [x] `_indefinite_by_node: Dict[node, rid]`
  - [x] `commit(rid, path, start_time=0) -> bool` (atomic + 자기 자신 통과)
  - [x] `release(rid, fire_callbacks=True)`
  - [x] `is_free(node, time, exclude_rid=None)`
  - [x] `is_edge_free(from, to, time, exclude_rid=None)` (swap collision 차단)
  - [x] `advance(rid, current_node, t)` (과거 자동 정리)
  - [x] `reserve_indefinite(rid, node)` (idempotent)
  - [x] `on_release(callback)`
- [x] 2.2 `tests/test_reservation.py` 신규 — 18개 테스트 통과
- [x] 2.3 `RequestHandler.__init__`에 `self.reservation = ReservationService()` 추가
- [x] 2.4 pytest 49 passed (기존 31 + 신규 18). 행동 변화 0 확인

### Phase 3 — Planner를 ReservationService 위로 (1~1.5일)
- [x] 3.1 `astar_with_time` 시그니처 변경 — `reservation` + `rid` 인자 추가 (legacy `reserved_nodes`/`edges`는 옵셔널로 유지 → 다른 호출 사이트 안 깨짐)
- [x] 3.2 알고리즘 본체에서 `reservation.is_free()` / `is_edge_free()` 호출 (use_reservation 분기)
- [x] 3.3 `_plan_and_publish_move`: 다른 로봇 planned_path를 `self.reservation`에 snapshot 동기화 (dwell=1 = legacy +1 timing 버퍼). 결과 path 자기 commit
- [x] 3.4 pytest 회귀 (52 passed; ReservationService dwell 단위 테스트 3개 추가) — **시뮬 사용자 확인 완료 (2026-06-04, "지금 상태로 괜찮음")**

### Phase 4 — 사후 대응 1개씩 삭제 (1~1.5일)
각 단계 후 회귀 + commit:
- [x] 4.1 `_lookahead_replan` 삭제 (매 마커 사후 검사) — 본체 39줄 + 호출부 4→1줄. 카운터 dict 키는 유지(0 출력) — Phase 8.3 비교용
- [x] 4.2 `_should_hold_for_eta` 삭제 (ETA hold) — 2026-06-04. 헬퍼 3종(`_should_hold_for_eta`/`_estimate_exit_steps`/`_estimate_path_cost`) + 호출부 ETA 분기 제거. 점유 중이면 항상 staging 우회로 복귀(baseline=1, correctness 영향 X). 카운터 키 유지(0 출력). pytest 52 passed. DISPATCH_FLOW.md 갱신
- [~] 4.3 `_resolve_deadlock` 전략 1/2 삭제 (이동 중 정면 교착 yield) — 2026-06-04, **옵션 A (부분)**
  - 제거: `_resolve_deadlock` 전략 1(alt-path) + 전략 2(yield-node) → `return`으로 대체 (edge 예약 is_edge_free가 plan 시점 swap 차단, I2)
  - **유지(이월)**: `_find_yield_node`(staging 285/goal-lock 317이 아직 씀), goal-lock 분기, staging yield 분기, `_resolve_deadlock` 트리거(`_try_dispatch_all` 88~97)
  - 이유: 대체재 있는 net만 제거. goal-lock 대체재(reserve_indefinite)는 4.4, staging은 4.5
  - 테스트: obsolete 2개 삭제(`test_head_on_deadlock_yield_robot_replans`/`test_carrying_robot_priority`) — 대체재는 `test_reservation.py::test_swap_collision_blocked`가 검증. staging yield 테스트 유지. **pytest 50 passed**
  - **시뮬 검증 대기**: edge 예약만으로 이동 교착 안 나는지 (동시 시작 head-on 시나리오)
- [x] 4.4 goal-lock 삭제 — 2026-06-05, **옵션 A (reserve_indefinite 안 씀)**
  - 제거: `_deferred_goals`(init) + `_check_goal_locked_robots`(메서드+호출) + `_resolve_deadlock` goal-lock 분기 + IDLE-swap(goal-lock 전용 setup) + goal_lock 카운터 증가. `_resolve_deadlock`은 이제 **staging yield 전용**으로 축소
  - **reserve_indefinite 미사용 결정**: (1) snapshot resync 모델이 매 plan마다 indefinite 예약을 release로 소멸 → 충돌, (2) 정차 로봇 회피는 `excluded_transit`(Layer 1.2)가 이미 함 = 중복, (3) goal 해제 후 재계획(wake-up)은 `_try_dispatch_all` 재시도가 이미 매 마커/ack마다 제공. → reserve_indefinite 신규 배선은 실익 없음. goal 막힌 로봇은 **제자리 대기 → blocker 이탈 시 자동 진행**
  - **유지(이월)**: `_find_yield_node`(staging 분기가 아직 씀 → 4.5), staging yield 분기(→4.5), `_resolve_deadlock` 트리거
  - **pytest 50 passed** (staging yield 테스트 유지·통과)
  - **시뮬 검증 대기**: goal 막힌 로봇이 제자리 대기 시 blocker 퇴로 막아 상호 교착 안 나는지 (baseline goal_lock=0이라 희귀)
- [ ] **4.5 staging 큐 → 예약 재구현** (큰 공사, 별도 브랜치 권장 `refactor/staging-reservation`). 설계 ↓
  - [x] 4.5.0 **토대** (2026-06-05) — `release(keep_indefinite=)` 추가. `commit` 내부 release + snapshot resync 2곳이 `keep_indefinite=True`. **행동 변화 0** (reserve_indefinite 호출자 아직 없음 → 분기 미실행). 테스트 3개(test_release_keep_indefinite_preserves_corridor / test_recommit_preserves_own_indefinite / test_full_release_clears_indefinite_for_wakeup). **pytest 53 passed**. 시뮬 불요(런타임 동작 불변)
  - [x] 4.5.1 corridor 점유 = `reserve_indefinite(occupant, **ws_node**)` 이중기록 (2026-06-05) — 회랑=WS 노드 하나 (gateway는 통과 길목, 미포함). StagingManager에 `set_reservation` 주입 + `_sync_occupancy(ws_node)` 헬퍼를 점유변경 5곳(should_stage 진입/release_without_trigger 2분기/mark_exiting/preempt/timeout 2분기)에 호출. ReservationService에 `release_indefinite_node` + `reserve_indefinite` transfer 정리(`_drop_indef_entry`) 추가. **상태기계가 여전히 권위, reservation은 그림자**(fire_callbacks=False). 4.5.0 keep_indefinite가 resync에서 보존. **pytest 55**. **시뮬 검증 대기** (WS 차단이 excluded_transit와 중복이라 delta 작음 예상)
  - [x] 4.5.2 **reader-flip #1** (2026-06-05) — `should_stage`의 "회랑 비었나?" 판단을 `corridor.state` → `reservation.is_corridor_held(ws_node)`로 전환. **새 장부로 내리는 첫 결정.** ReservationService에 `is_corridor_held(node, exclude_rid)` 추가 (indefinite만 보고 cell/edge 무시). reservation 미주입 시 corridor.state fallback. 이중기록(4.5.1) 덕에 동치 → **동작 불변 기대**. **pytest 57**. **시뮬 검증 대기**
    > [순서 조정] 체크리스트 원래 4.5.2=wake-up이었으나, wake-up이 4갈래 분기로 제일 어려운 조각이라 strangler 정석대로 **쉬운 reader-flip부터**. wake-up은 4.5.4로 뒤로 미룸.
  - [x] 4.5.3 **reader-flip #2** (2026-06-05) — "누가 회랑 주인?" 결정 읽기 3곳(`should_stage` already-authorized / `release_corridor_without_trigger` guard / `check_position_release`)을 `corridor.occupying_rid` → `self._owner(ws_node)`로. ReservationService `corridor_owner(node)` + StagingManager `_owner()`(reservation 우선, 미주입 fallback). preempt(239)는 4.5.5로 이월. **pytest 58**. **시뮬 검증 대기** (동작 불변 기대)
  - [x] 4.5.4a (2026-06-05) 깨우기 복붙 dispatch → `_dispatch_released_agv` 헬퍼 추출 (_marker_mixin). 순수 추출, 동작 0. pytest 58.
  - [~] ~~4.5.4b/c (on_release 트리거 전환)~~ **폐기** — 하이브리드(아래)로 선회. 큐를 살리므로 on_release 콜백 불필요. 현 마커-기반 트리거 유지.
  - [ ] **4.5.5 (하이브리드) 점유 중복 제거** — `CorridorInfo.state`/`occupying_rid` 삭제 → 점유는 reservation 단일 진실. **큐·is_exiting·timeout·깨우기 트리거 유지.**
    - [x] 4.5.5a (2026-06-05) 남은 점유 *읽기* flip 4곳: preempt guard+owner / `check_position_release` state(중복이라 제거) / `_check_timeout` guard → 전부 `self._owner(ws_node)`. 남은 corridor.state 읽기 = 116(sync 소스, 4.5.5b서 쓰기로) + 165(fallback)뿐. **pytest 58**. 시뮬 검증 대기 (동작 불변 기대)
    - [ ] 4.5.5b 점유 *쓰기*(`corridor.state=`/`occupying_rid=` 전부)를 reservation 호출로 → `_sync_occupancy`가 "거울"이 아니라 실 저장소. 그 후 `state`/`occupying_rid` 필드 + `CorridorState` enum 삭제
    - [ ] 4.5.5c `get_status_summary` 등 표시도 reservation 파생
  - [ ] 4.5.6 staging yield(`_resolve_deadlock` 잔여) + `_find_yield_node` + `_yielded_staging_robots` 제거 — **단, 4.3/4.4처럼 "reservation이 staging 교착을 plan 시점에 막나" 분석 먼저** (`test_staging_blocker_forces_yield`가 실 시나리오 → 자동 제거 X)

#### 4.5 설계 — 점유는 reservation, 큐는 유지 (하이브리드)

> **설계 결정 (2026-06-05, 사용자)**: corridor를 reservation으로 *완전* 흡수(큐까지 제거 = "방법 2")하려던 원안 **폐기**. 사용자 철학 **"플로우는 하나, 재료는 갖다쓰기"**로 재검토한 결과:
> - **점유(`state`/`occupying_rid`)** = reservation에도 있는 정보의 *복사본* = 진짜 그림자. 두 장부가 엇갈려 cascade race(수정 46) 유발. → **제거, reservation 단일 진실에서 갖다쓰기.**
> - **큐(대기 순서)** = reservation에 없는 *고유 재료*(그림자 아님). "다음 대기자를 큐에서 갖다쓰기"는 철학에 부합. → **유지.**
> - **on_release 콜백** = 큐가 살아있으면 불필요(현 마커-기반 트리거가 이미 동작). 방법 2의 "위치 스캔"은 깨끗한 재료(큐) 두고 플로우가 매번 재계산 = 철학에 *덜* 맞음. → **폐기.**

**수용하는 작은 부담**: 큐는 robot task 변경 시 stale 엔트리 청소(`remove_robot_from_queues`) 필요. 점유 중복(매 변경 **2곳** 갱신)에 비하면 훨씬 작고 한 곳에 모임. 2대 환경에서 거의 무해.

**상태 매핑 (수정판)**:
```
현재 (CorridorInfo)            →  하이브리드
state / occupying_rid          →  삭제. reservation indefinite (is_corridor_held / corridor_owner에서 갖다씀)
queue                          →  유지 (대기순서 = 고유 재료)
occupied_at                    →  유지 (CorridorInfo 잔존, 타임아웃용)
is_exiting / mark_exiting      →  유지 (퇴출 phase 플래그)
깨우기 트리거                   →  유지 (마커-기반: check_position_release / handle_marker_trigger / 인터셉트)
```
회랑 점유 노드 = **WS 노드 1개** (4.5.1 결정; gateway는 통과 길목). staging_node는 corridor 밖이라 대기 AGV는 거기서 자연 정차.

**진행 (4.5.1~3 위에 쌓음)**:
- 점유 *읽기*는 이미 reservation: `is_corridor_held`(4.5.2) / `corridor_owner`(4.5.3)
- 남은 것 (4.5.5): preempt·timeout 읽기 flip → 점유 *쓰기*를 reservation 호출로 → `state`/`occupying_rid` 필드 + `CorridorState` enum 삭제
- 깨우기는 4.5.4a로 헬퍼 단일화 완료 — 트리거는 그대로 (on_release 안 씀)

**전제조건 (4.5.0 완료)**: snapshot resync가 indefinite를 지움 → `keep_indefinite=True`로 보존. 점유가 reservation 단일 진실이 되면 이게 필수.

**리스크**: cascade race 핫스팟(수정 23/37~44/46). 점유 쓰기를 옮길 때(4.5.5b) 매 단계 시뮬 필수.

### Phase 5 — 도메인 SM 명시화 (1일)
- [ ] 5.1 `Robot.set_status(new, reason)` 추가
- [ ] 5.2 `Shelf.set_status(new, reason)` 추가
- [ ] 5.3 `robot.status = X` 직접 대입 grep → 전수 변환
- [ ] 5.4 `shelf.status = X` 직접 대입 grep → 전수 변환
- [ ] 5.5 잘못된 전이 시 `InvalidTransitionError` 발생

### Phase 6 — main.py docstring + mixin 섹션 주석 (반나절)
- [ ] 6.1 `server/main.py` 상단 docstring
  - [ ] 이벤트 ↔ 핸들러 ↔ 다이어그램 노드 매핑 표
  - [ ] 재료 9종 목록 + 역할
  - [ ] 대표 호출 시퀀스 (start_order 따라가기)
- [ ] 6.2 `_workflow_mixin.py` 섹션 주석 (다이어그램 노드 매핑)
- [ ] 6.3 `_marker_mixin.py` 섹션 주석
- [ ] 6.4 `_movement_mixin.py` 섹션 주석

### Phase 7 — Real-world 안전망 (1~1.5일)
- [ ] 7.1 Marker timeout — 마지막 마커 후 N초 무응답 시 alert + AGV FAILED 마킹
- [ ] 7.2 위치 desync 자동 복구 — `actual != expected` → release + 재plan
- [ ] 7.3 Reservation timeout — N분 활성화 안 되면 자동 release (좀비 예약 방지)
- [ ] 7.4 운영 모니터링 endpoint — reservation/AGV health 조회

### Phase 8 — 풀스택 시연 검증 (1~2일)
- [ ] 8.1 회귀 테스트 31+ pytest 통과
- [ ] 8.2 4 시나리오 자동 체인 통과 (포워딩/인터셉트/staging/PICK차단)
- [ ] 8.3 Phase 1 baseline 대비 사후 대응 트리거 카운트 **0** 확인
- [ ] 8.4 Divergence 시나리오 의도적 재현 (마커 누락, 잘못된 노드) → 자동 복구 확인
- [ ] 8.5 라파 + 노트북 + Isaac Sim 풀스택 시연
- [ ] 8.6 갱신된 FLOWCHART.md ↔ 코드 1:1 대응 확인

---

## 핵심 아이디어 요약

### 사용자 모델 (보존)
> "재료를 만들어놓고, 필요할 때 갖다 쓴다. 재료끼리 서로 참조한다. 재료만 한 번 잘 만들면 새 시나리오에서 변경 안 함."

이번 리팩토링 = **재료를 한 번 잘 만드는 그 한 번**.

### 핵심 invariant 5개
- **I1** 미래 점유 = ReservationService만
- **I2** 예약 충돌은 plan 시점에만
- **I3** Staging = 노드 group의 시공간 예약
- **I4** Replan은 환경 변화 이벤트만 (+ 마커 desync)
- **I5** 상태 전이 = SM 메서드만

### 사후 대응 7종 (다이어그램에 없는 것들, 삭제 대상)
1. Lookahead replan (매 마커 사후 검사)
2. Deadlock yield 전략 1
3. Deadlock yield 전략 2 (yield_node)
4. Goal-lock 감지/대기
5. ETA hold
6. Staging redirect + corridor preempt
7. Staging cascade wake-up

### 흩어진 상태 → 대체 매핑 (2026-06-05 수정 — 원안의 "과잉 순수화" 교정)

> [교훈] 원안은 *모든* 상태를 reservation으로 녹이려 했으나, **그림자(중복) ≠ 고유 재료**를 구분 안 함. 사용자 철학 "재료는 갖다쓰기"상 **중복만 제거, 고유 재료는 유지**가 맞음. 아래는 교정판.

| 변수 | 성격 | 대체/처리 |
|------|------|------|
| `_reserved_nodes` | 중복(미래 점유) | `reservation.cells` 직접 ✅ |
| `_blocked_robots`, `_lifting_robots`, `_in_flight_cmds` | — | REFACTOR E CommandQueue ✅ |
| `_goal_locked_robots` | 반응형 net | **✅ 4.4 제거** — ~~reserve_indefinite+on_release~~ 안 씀. `_try_dispatch_all` 재시도로 충분 |
| `_deferred_goals` | 반응형 net | **✅ 4.4 제거** (동상) |
| `_yielded_staging_robots` | 반응형 net | 4.5.6에서 staging yield와 함께 — **단 "reservation이 staging 교착 막나" 분석 후** (자동 제거 X) |
| `_staged_to_ws` | **고유**(early-release desync 보류) | ~~사라짐~~ **재검토 필요** — corridor 점유 복사본 아님. 큐 유지 시 사라질 근거 약함. case-by-case |
| `staging_manager.queue` | **고유**(대기 순서) | ~~on_release~~ **유지** (그림자 아님). 점유만 reservation으로 (하이브리드) |
| `robot.planned_path` "진실" | 중복(시간 점유) | 격하 (캐시뷰), reservation이 진실 |

### Real-world divergence — Marker = Ground Truth
```python
def _on_marker(self, rid, marker_id):
    robot.current_node = marker_id  # 진실
    expected = robot.planned_path[0] if robot.planned_path else None
    if expected and marker_id != expected:
        # 경로 어긋남 → 자동 복구
        self.reservation.release(rid)
        new_path = self.path_planner.find_path(rid, marker_id, robot.goal, self.reservation)
        self.reservation.commit(rid, new_path)
        return
    # 정상 진행
    robot.planned_path = robot.planned_path[1:]
    self.reservation.advance(rid, marker_id, t=now)
```

---

## 결정 이력 (Phase 진행 중 만든 의사결정 기록)

### 시작 시점 (2026-05-27)
- WHCA* 윈도우 모델 → **단순 full path commit으로 변경** (8×6 환경에 충분, 더 단순)
- `accepted` 토픽은 수정 50으로 롤백됨 (DB 접근 제거만 유지). 이 리팩토링은 별개 영역
- 새 브랜치 `refactor/path-reservation-service` 사용. main 시연 영향 0
- 진행 중 발견하는 결정은 여기에 추가

### Phase 0 결정
- (Phase 0 진행 시 기록)

### Phase 2 결정
- (Phase 2 진행 시 기록)

---

## Phase 1 Baseline (측정 결과)

### 2026-06-02 부분 측정 (4 시나리오 자동 체인 일부, 협업자 warehouse_gui + server 같이 구동)

```
lookahead_replan         : 53
resolve_deadlock         : 0
find_yield_node          : 0
should_hold_for_eta      : 1
staging_redirect         : 2
goal_lock                : 0
staging_cascade          : 6
```

### 해석

| 카운터 | 값 | 해석 |
|--------|-----|------|
| `lookahead_replan` | **53 (압도적)** | 매 마커 도착마다 호출. Phase 4.1 삭제가 가장 큰 충격 — A*+reservation이 매 마커 사후 검사 안 해도 충돌 없어야 함 |
| `resolve_deadlock` | 0 | 실제 deadlock 트리거 없음. lookahead_replan이 미리 잡아서? 아니면 부분 시나리오라 미발생? |
| `find_yield_node` | 0 | 동일. yield_node 전략은 deadlock 발생 시에만 |
| `goal_lock` | 0 | goal-lock 케이스 미발생 |
| `should_hold_for_eta` | 1 | ETA hold는 매우 드물게 발화 (수정 44 케이스) |
| `staging_redirect` | 2 | staging 우회는 종종 발생 (정상) |
| `staging_cascade` | 6 | 포워딩/release_corridor_without_trigger 호출 (정상) |

### 시사점
- **lookahead_replan = 53** 이 핵심 의존성. Phase 4.1에서 이걸 빼면 53개의 "사후 안전망"이 사라짐 → ReservationService(I1/I2)가 plan 시점에 100% 차단해야 함
- **resolve_deadlock/find_yield_node/goal_lock = 0** 은 두 가지 해석 가능:
  - (A) 실제로 거의 안 쓰임 → 삭제 안전
  - (B) 부분 시나리오라 미발생 → 풀 시나리오에서 다시 측정 필요
- **부분 측정이라 staging_cascade=6, staging_redirect=2는 풀시 더 늘 수 있음**

### 풀 측정 권장 (선택)
4 시나리오 자동 체인 끝까지 + DEMO_MODE=False 시연 1회 추가하면 더 정확. 하지만 Phase 2 진입엔 부분 측정도 충분 (어차피 Phase 8.3에서 다시 측정해서 비교)

---

## 다음 세션 entry 포인트

> 세션 끝낼 때 여기 갱신. 새 세션 시작 시 여기부터 확인.

**현재 단계**: **Phase 4.1 완료**. 사용자 시뮬 검증 대기.
**마지막 작업**: `_lookahead_replan` 본체 39줄 + 호출부 단순화. pytest 52 passed.
**다음 작업**: 사용자 시뮬 — 4 시나리오 자동 체인 + Ctrl+C 시 카운터에서 `lookahead_replan: 0` 확인 + 시각적 동일 동작. 깨지면 ReservationService snapshot 모델 보강 필요. 통과하면 Phase 4.2~4.5 묶음 진입.
**알려진 막힘 / 의문**: lookahead가 catch하던 케이스 (다른 AGV가 mid-task에 IDLE로 내 path 위 정차) — `_is_blocked` (cmd 발행 직전 점유 체크) + `_resolve_deadlock` (남아있음) 이 2중 안전망으로 충분한지가 시뮬 검증 포인트.

---

## 자주 참조하는 파일

- 전체 plan: `~/.claude/plans/gleaming-enchanting-lerdorf.md`
- 현재 워크플로우: `FLOWCHART.md` (Phase 0 대상)
- REFACTOR E (이미 진행됨): `server/REFACTOR_E.md`
- 다이어그램 핵심: `server/DISPATCH_FLOW.md`
- 메모리: `~/.claude/projects/-home-won-ububtu-Desktop-Projects/memory/refactor_f.md`

---

## 작업 패턴

### 새 세션 시작 시
1. 이 파일 (REFACTOR_F.md)의 "다음 세션 entry 포인트" 확인
2. 진행 체크리스트에서 현재 Phase 위치 확인
3. "결정 이력" 빠르게 훑어서 컨텍스트 복원
4. 작업 시작

### 세션 끝낼 때
1. 진행한 항목 `[ ]` → `[x]` 체크
2. 결정 사항 있으면 "결정 이력"에 추가
3. "다음 세션 entry 포인트" 갱신
4. (사용자가) git add + commit

### Phase 완료 시
1. 모든 sub-task 체크
2. 회귀 테스트 통과 확인
3. 메모리 `refactor_f.md`에 Phase 완료 요약 추가
4. 다음 Phase 진입 표시
