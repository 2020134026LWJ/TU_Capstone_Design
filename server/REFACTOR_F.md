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
- [ ] 4.3 `_resolve_deadlock` + `_find_yield_node` 삭제 (deadlock yield)
- [ ] 4.4 `_goal_locked_robots` + `_deferred_goals` 삭제 (goal-lock)
  - [ ] `reserve_indefinite` + `on_release`로 대체
- [ ] 4.5 staging 큐 관리 삭제 (`staging_manager` 정적 헬퍼로 격하)
  - [ ] `add_staged_agv` 삭제
  - [ ] `release_corridor_without_trigger` 삭제
  - [ ] `pop_next_in_queue` 삭제

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

### 흩어진 상태 10종 → ReservationService 대체 매핑
| 변수 | 대체 |
|------|------|
| `_reserved_nodes` | `reservation.cells` 직접 |
| `_blocked_robots`, `_lifting_robots`, `_in_flight_cmds` | REFACTOR E CommandQueue |
| `_goal_locked_robots` | `reservation.reserve_indefinite` + `on_release` |
| `_deferred_goals` | `reservation.on_release` 콜백 |
| `_yielded_staging_robots` | 사라짐 (yield가 A*에 흡수) |
| `_staged_to_ws` | 사라짐 (corridor가 예약의 일부) |
| `staging_manager.queue` | `reservation.on_release` 이벤트 |
| `robot.planned_path` "진실" | 격하 (캐시뷰) |

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
