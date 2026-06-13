# 서버-AGV 구조 재정비 계획

## 의도한 아키텍처

**철학: AGV는 깡통, 서버가 두뇌.**

### AGV 역할 (sensor + actuator만)
- 카메라로 ArUco 마커 감지 → 서버로 `marker_id + heading` 보고
- 서버 명령(`forward`/`turn_left`/`turn_right`/`turn_180`/`lift_up`/`lift_down`) 수신 → 단순 실행
- 완료 시 `cmd_ack` 또는 다음 마커 발행
- 자율 판단 없음

### 서버 역할 (모든 결정)
- 주문 스케줄링, 선반 순서 최적화
- A* 경로 계산
- **매 노드 도착마다 앞 경로가 막혔는지 / 막힐 예정인지 확인 → 필요 시 사전 재계획**
- 명령 발행
- 충돌/교착 회피

---

## 현재 구현 vs 의도 — 갭 분석

### 갭 1. AGV 명령 수신 채널이 단일 슬롯

`step6_visual.py:179` — `_pending_cmd: str | None`. 명령이 빠르게 두 개 들어오면 두 번째가 첫 번째를 덮어씀.

| 의도 | 현재 |
|---|---|
| 받은 명령을 순서대로 실행 | 단일 슬롯 → 덮어쓰기 가능 |

→ **의도와 다른 게 아니라 구현 결함.** AGV는 여전히 깡통이지만, 신뢰성 있는 명령 채널이 빠짐.

### 갭 2. 매 노드 사전 검증 — 깊이가 1 step뿐

| 의도 | 현재 |
|---|---|
| 매 step에 앞으로 N 노드 검사 | 다음 **1 노드만** 검사 (`_send_next_command`) |
| 다른 AGV의 미래 경로(`planned_path`) 교차 검사 | `current_node` + 1-step `_reserved_nodes`만 |
| 충돌 예상 시 사전 replan | 충돌 발생 후에야 replan (`_resolve_deadlock`) |

검증 깊이가 얕아 멀리 있는 충돌을 놓침. 발생한 뒤에야 수습.

### 갭 3. `planned_path` 동기화 안 됨

| 상태 | 갱신 시점 |
|---|---|
| `robot.current_node` | 마커 도착마다 (실시간 ✓) |
| `robot.planned_path` | plan 발행 시 1회 (이후 박제 ✗) |

A*의 시간 예약은 `planned_path` 기반인데, 이게 영원히 plan 시점 그대로. 시간축이 stale.

### 갭 4. 주차 AGV가 장애물로 등록 안 됨

`_movement_mixin.py:531-534` — `planned_path`가 빈 로봇(IDLE/주차)은 현재 위치를 t=0~2만 예약. 그 이후 시간대엔 A*가 비어있다고 봄.

| 의도 | 현재 |
|---|---|
| 주차 차량은 영구 장애물 → 다른 AGV가 우회 | 주차 차량 위로 plan → 충돌 → yield시켜 깨움 |

---

## 발견한 버그들 (갭과 매핑)

| ID | 증상 | 근본 원인 | 관련 갭 |
|---|---|---|---|
| A | AGV-1이 25(AGV-2 주차) 위로 경로 plan | 주차 차량이 t=0~2만 예약 | 갭 4 |
| B | 주차된 AGV가 yield되어 빈 차로 움직임 | 갭 4 → deadlock_resolve가 주차 차량을 옆 칸으로 보냄 | 갭 4 |
| C | turn_180 명령 유실 → AGV가 엉뚱한 방향 이동 (18→19) | `_pending_cmd` 단일 슬롯 덮어쓰기 + 서버가 ack 안 기다리고 다음 cmd 발행 | 갭 1 |
| D | 포워딩 시 동쪽으로 빙 도는 비효율 경로 | stale `planned_path` 시간축 + turn_penalty 부정확 | 갭 3 |
| E | Deadlock cascade (yield → goal-locked → 왕복) | 갭 3 + 갭 4의 합성, 잘못된 plan이 반복적으로 충돌 발생시킴 | 갭 3, 4 |

---

## 해결 방향

### Layer 1 — 의도된 동기화 회복 (최우선)

#### 1.1 `planned_path` slide ✓ 적용됨 (수정 32)
- 마커 도착 시 `planned_path`를 current_node 기준으로 앞에서 자르기
- 위치: `_marker_mixin.py:43` 직후 (단, `calc_heading_from_path` 호출 **이후**에 trim — heading fallback 보호)
- 효과: A* 시간 예약이 실제 진행과 정합. → **갭 3 해결 → 버그 D, E 완화**

```python
# _marker_mixin.py heading 계산 뒤
if node in robot.planned_path:
    idx = robot.planned_path.index(node)
    robot.planned_path = robot.planned_path[idx:]
```

#### 1.2 IDLE 영구 장애물 등록 ✓ 적용됨 (수정 32)
- `_movement_mixin.py:531-534`의 `range(3)` 예약을 `excluded_transit`에 추가로 교체
- 효과: A*가 주차 차량 위로 경로 안 그림. → **갭 4 해결 → 버그 A, B 해결, E 대폭 완화**

```python
if not other.planned_path:
    excluded_transit.add(other.current_node)
```

`astar_with_time`의 `excluded_transit`은 start/goal은 검사 제외이므로 goal=주차 노드인 엣지 케이스도 안전.

#### 1.3 in-flight cmd 추적 (서버 측) ✓ 적용됨 (수정 34)
- 서버가 각 AGV의 "마지막 발행 cmd + ack/marker 미수신" 상태 관리 (`_in_flight_cmds: Dict[int, str]`)
- ack/marker 도착 전까진 다음 cmd 발행 보류 (`_blocked_robots.add` → 자동 재시도)
- 위치: `_movement_mixin.py:_send_next_command` 진입 가드 + `_marker_mixin.py`의 marker/cmd_ack 핸들러에서 pop
- 효과: → **갭 1 해결 → 버그 C 해결**

### Layer 2 — 매 노드 사전 검증 (의도된 lookahead) ✓ 적용됨 (수정 33)

#### 2.1 `_lookahead_replan(rid)`
- 마커 도착 시 자신의 planned_path 전체를 검사
- 검사 항목:
  - IDLE 로봇이 내 경로 위에 정차 → 영구 장애물
  - 다른 AGV의 slide된 planned_path[i]와 내 planned_path[i] 동일 → 시간 교차
- 충돌 예정 발견 → 즉시 `_plan_and_publish_move`로 우회 경로 채택
- 위치: `_movement_mixin.py:_lookahead_replan` + `_marker_mixin.py`에서 `_send_next_command` 직전 호출
- 효과: 충돌 발생 전에 우회. `_resolve_deadlock` 호출 빈도 급감.

### Layer 3 — 안전망 (기존 유지)

`_blocked_robots`, `_resolve_deadlock`, yield 로직 — Layer 1+2가 못 잡는 엣지 케이스용으로 그대로 두기.

---

## 알려진 후속 이슈 (Layer 1+2 적용 후 관측)

### 이슈 F: yielded-staging displacement 과대 (2026-05-21 관측)
- **증상**: AGV-2가 W9 staging(node 1)에 대기 중일 때, 다른 AGV에 밀려서 7 step 떨어진 node 29까지 displacement 됨 (1→2→3→4→5→13→21→29).
- **위치**: `staging_manager.py` + `_movement_mixin.py`의 staging-yield 로직 (수정 28에서 추가된 안전망)
- **영향**: 시연 안정성엔 무관. 단지 빈 차로 멀리 이동하는 비효율.
- **우선순위**: 낮음. Layer 1+2와 무관한 별도 메커니즘.
- **메모**: staging deadlock 안전망이 너무 공격적으로 displacement 결정하는 듯. 옆 칸 1개만 yield하도록 제한 검토 필요.

### 이슈 G: 포워딩 시 staging 노드 위치 (Dynamic 분리 적용 — 2026-05-21)
- **문제**: 포워딩 AGV가 목적지 corridor 점유 중일 때 멀리 있는 staging_node(1, 41)로 우회 → 선반 들고 9 nodes 우회하는 비효율
- **사용자 제안**: 포워딩 시 1/41 대신 17/25(gateway)에서 대기
- **적용 = Option A (Dynamic 분리)**:
  - `should_stage(..., is_forwarding=True)` → gateway 반환 / `False` → 기존 staging_node 반환
  - `_plan_and_publish_move(..., is_forwarding=)` 파라미터 추가, 포워딩 콜에서 `True` 전달
  - `is_staged_agv`, `get_ws_for_staging_node` 큐 기반 조회 (gateway에서도 staging 인식)
  - `excluded_transit`에 `_is_staging_robot(other_rid)` 추가 → outbound가 staging AGV를 영구 장애물로 보고 우회
- **수정 파일**: `staging_manager.py`, `_movement_mixin.py`, `_workflow_mixin.py`

---

## Dynamic vs Strict 분리 (보류 — 향후 시연 안정성 보강 시 검토)

이슈 G 해결 과정에서 두 가지 접근 비교. 현재는 **Dynamic** 채택, 검증 후 부족하면 **Strict** 보강.

### Dynamic 분리 (현재 적용)
- 포워딩만 명시적으로 `is_forwarding=True` 전달 → gateway에서 대기
- 나머지 트래픽은 cooperative A*가 자유 선택
- 충돌 위험은 `excluded_transit` (staging AGV 영구 장애물)로 차단
- **장점**: 경로 효율 최대, 코드 변경 최소
- **단점**: 그때그때 다른 경로 → 시연 시 설명 어렵고, 예측 불가

### Strict 분리 (보류)
- 정적 traffic 분리: gateway(17, 25) = 포워딩 전용, staging/trigger = 나머지
- 포워딩 vs 포워딩 동시 진입 시 yield로 trigger 우회
- **장점**: 시연 발표 시 "이 통로는 이런 용도" 명확, 항상 같은 패턴
- **단점**:
  - 비-포워딩 outbound가 항상 trigger 통과 → 약간 우회
  - **모순**: gateway가 idle 주차지(AGV-1=17, AGV-2=25)로 쓰임 → "포워딩 전용 통로에 IDLE AGV 박힘"
  - 해결하려면 주차지 이동 필요 (예: 17→18, 25→26)
  - 코드 변경: ① 비-포워딩 plan에서 gateway를 excluded_transit 추가 / ② robot_config 주차 노드 변경 / ③ 수정 30의 idle 주차 로직 조정

### 결정
- **현재**: Dynamic 유지, 4 시나리오 시연 검증
- **추후**: 시연 시 경로 예측 불가/설명 어려움이 문제로 드러나면 Strict + 주차지 이동으로 보강

---

## 적용 순서

| 단계 | 작업 | 코드 변경량 | 기대 효과 | 상태 |
|---|---|---|---|---|
| **1** | Layer 1.1 + 1.2 동시 적용 | 약 5줄 | 버그 A, B, D, E 대부분 해결 | ✓ 수정 32 |
| **2** | Layer 1.3 (in-flight cmd 추적) | 약 20줄 | 버그 C 해결 | ✓ 수정 34 |
| **3** | Layer 2 (N-step lookahead) | 약 30~50줄 | 의도된 아키텍처 완성, 안정성 ↑↑ | ✓ 수정 33 |
| 부수 | turn_penalty 튜닝, goal 도달 시 `planned_path = []` | 미세 | 경로 최적성 ↑ | 보류 |

---

## 검증 시나리오

기존 4 시나리오(포워딩 / 인터셉트 / staging / PICK차단) 재실행하여 비교:

- [ ] `Deadlock (alt-path)`, `Goal-locked` 로그 감소/소멸
- [ ] AGV가 주차 노드 위로 경로 그리는 현상 소멸
- [ ] turn 후 forward 시 회전 빠짐 현상 소멸
- [ ] 빈 차로 움직이는 현상 소멸
- [x] 회귀 테스트 (`pytest`) 통과 유지 — 21 passed (2026-05-21)
- [ ] 포워딩 경로가 직선/최단으로 잡힘 (동쪽 우회 사라짐)

---

## 메모

- 갭 1, 4가 가장 임팩트 크고 코드 변경 적음 → 1단계가 가성비 최고
- 갭 2(lookahead)는 의도된 모습이지만 갭 3, 4가 풀려야 깔끔하게 동작 — 순서 중요
- 실물 전환 고려 시: 결함 1은 서버 측에서 해결해두는 게 펌웨어 부담 분리 차원에서 유리

