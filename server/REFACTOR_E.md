# REFACTOR E — ACK 기반 단일 명령 큐 (3단계 근본 리팩토링)

> **목표**: 의미적 게이트 9종 → 구조적 invariant로 통합. race 카테고리 자체를 구조적으로 제거.
> **예상 작업량**: 1.5~2일 (카테고리 A+D만) / 3~5일 (전 범위)
> **결정 (2026-05-26)**: **전 범위 진행** (B/C 포함). 어차피 다 정리해야 함.

---

## 진행 체크리스트 (세션 재진입 시 여기부터 확인)

- [x] 1.1 게이트 9종 전수 조사
- [x] 1.2 ACK 큐 invariant + 자료구조 설계
- [x] 1.3 마이그레이션 플랜
- [x] 2.1 `command_queue.py` 모듈 + 클래스 (unit test)
- [x] 2.2 `RequestHandler.__init__` 큐 초기화
- [x] 2.3 `_send_next_command` 큐 기반 재작성 (병행 모드 + assert 일치)
- [x] 2.4 마커/cmd_ack 핸들러 `queue.ack()` 추가 (병행 모드)
- [x] 3.1 `_reserved_nodes` 제거
- [x] 3.2 `_in_flight_cmds` 제거
- [x] 3.3 `_blocked_robots` + `_retry_blocked_robots` 제거
- [x] 3.4 `_lifting_robots` 제거
- [x] 3.5 `planned_path` invariant 정착 (**시뮬 멈춤 버그 자연 해결**)
- [x] 4.1 `_goal_locked_robots` + `_deferred_goals` 통합
- [ ] 5.1 `StagingStateMachine` 클래스 설계
- [ ] 5.2 `_yielded_staging_robots` + `_staged_to_ws` SM으로 흡수
- [x] 6.1 미사용 헬퍼 제거 (`_retry_blocked_robots` alias 삭제, unused typing imports 8종, ShelfStatus/CorridorState/SubTaskType/TaskStatus 미사용 import 제거, test/workflow docstring 옛 이름 정리)
- [ ] 6.2 시연 흐름 재검증
- [ ] 6.3 FLOWCHART.md 수정 50 기록 + 메모리 정리

---

## 배경

### 의미적 게이트 누적 증거
수정 30 이후 거의 모든 수정이 in-flight cmd race 또는 staging cascade race의 변종. 새 가드 추가 → 다른 곳 부작용 → 또 가드 추가. 누적 결과 `request_handler`에 게이트 9종.

수정 48이 "구조적"이라고 자평했지만 곧 부작용(parking 종료 후 planned_path 비우지 않음 → 가용 분류 영원히 실패) 발견 = 진짜 구조적 아니었던 증거.

### 시뮬 멈춤 버그 (2026-05-26)
사용자 1 주문 14 처리 중 5개 task 중 2개만 완료 후 정지. 원인: 수정 48 `not r.planned_path` 가드가 parking 종료 후에도 작동해 pending dispatch 영구 차단. 임시 우회 없이 이 리팩토링으로 자연 해결 목표.

---

## 1.1 게이트 9종 전수 조사 결과

### 카테고리 A — 명령 발행 제어 (4종)

| 게이트 | 무엇을 막는가 | set 시점 | clear 시점 | 의존성 |
|---|---|---|---|---|
| `_in_flight_cmds: Dict[int, str]` | ACK 전 후속 cmd 발행 → AGV `_pending_cmd` 슬롯 덮어쓰기 + 명령 유실 | `_send_next_command` 발행 성공 (L523) | `_handle_marker_report` (L73) / `_handle_cmd_ack` (L229) | `_blocked_robots`, `_predict_heading_after_inflight` |
| `_blocked_robots: Set[int]` | 충돌/in-flight 충돌 시 cmd 보류 → 재시도 큐 | `_send_next_command` 가드 실패 (L488/499/510) | 발행 성공 (L520) / `_retry_blocked_robots` | `_in_flight_cmds`, `_reserved_nodes` |
| `_reserved_nodes: Dict[int, int]` | forward 목적지에 타 로봇 진입 차단 | `_send_next_command` forward 발행 시 (L516) | `_handle_marker_report` (L70-71) | — |
| `_lifting_robots: Set[int]` | 리프트 중 deadlock yield → 빈 lift 차단 | pickup/putdown subtask 진입 시 (`_workflow_mixin` L359/398/668) | `_handle_cmd_ack` lift_up/lift_down (L241) | yield 로직 (`_resolve_deadlock`) |

**관찰**: 4종 모두 "AGV는 한 번에 1개 명령만 처리 가능 + 명령 lifecycle 추적 필요"라는 동일 요구의 분산 구현. 단일 큐로 통합 가능.

### 카테고리 B — Staging/회랑 동기화 (2종)

| 게이트 | 무엇을 막는가 | set 시점 | clear 시점 | 의존성 |
|---|---|---|---|---|
| `_yielded_staging_robots: Set[int]` | deadlock yield된 staging 로봇이 release 시 yield 위치(staging_node 아님)에서 plan | `_resolve_deadlock` yield 결정 시 (`_movement_mixin` L295) | corridor release 시 `_plan_and_publish_move` (`_marker_mixin` L102/166) | staging_manager, deadlock 해소 |
| `_staged_to_ws: Dict[int, tuple]` | 미리 release됐지만 staging_node 미도착 → desync 처리 | marker handler가 `released_robot.current_node != staging_node` 감지 (`_marker_mixin` L110/174) | staging_node 도착 마커 / corridor FREE 감지 (`_marker_mixin` L120-131) | staging_manager corridor state |

**관찰**: 둘 다 staging의 비동기 release 처리. staging state machine을 별도로 정리하면 두 게이트 모두 SM 내부 상태로 흡수 가능.

### 카테고리 C — Goal-lock (2종, 수정 30)

| 게이트 | 무엇을 막는가 | set 시점 | clear 시점 | 의존성 |
|---|---|---|---|---|
| `_goal_locked_robots: Set[int]` | goal 노드 점유자 이탈까지 yield + 대기 (무한 yield 루프 차단) | `_resolve_deadlock` Strategy 1/2 skip 조건 (`_movement_mixin` L326) | `_check_goal_locked_robots`가 blocker 이탈 감지 (L74/78/88) | `_deferred_goals` |
| `_deferred_goals: Dict[int, int]` | 위 robot의 보류된 goal 보관 | 위와 동기 (L327) | 위와 동기 (L79/89) | `_goal_locked_robots` |

**관찰**: 두 게이트는 항상 쌍으로 작동. `Dict[rid, goal]` 하나로 통합하면 끝 (set 멤버십 = key 존재 여부). 큐 시스템과는 독립이지만 deadlock 해소 정책 자체를 단순화 가능.

### 카테고리 D — Plan 진행 추적 (1종, 수정 32)

| 게이트 | 문제 |
|---|---|
| `planned_path` slide | 마커 도착 시 `path[idx:]`로 자름. **마지막 노드 도달 시 `[node]` 한 개 남고 비워지지 않음** → 수정 48의 `not r.planned_path` 가드가 영원히 작동 → pending dispatch 차단 |

**관찰**: invariant 누락. "마지막 도달 = path 비움" 명시 필요. 큐 도입 시 큐 길이로 대체 가능 (planned_path 의미 자체 단순화).

---

## 1.2 ACK 큐 invariant 설계

### 핵심 invariant (4개)

**I1 — 큐 단일성 (single source of dispatch)**
> AGV 1대마다 정확히 1개의 명령 큐. 모든 cmd 발행은 이 큐를 거침.

- 강제 방법: `mqtt_publisher.publish_cmd` 직접 호출 금지. 단일 발행점은 `CommandQueue.dispatch`(또는 그를 호출하는 `_send_next_command`)만.
- 위반 감지: grep으로 단일 발행점 확인 (CI에서 자동화 가능).

**I2 — 가용 정의 (availability)**
> robot이 가용 ⇔ `queue.in_flight is None and len(queue.pending) == 0`

- 기존 `RobotStatus.IDLE + heading_initialized` 유지하되, 수정 48의 `not r.planned_path` 가드를 `queue.is_idle()`로 대체.
- parking 종료 시 큐가 자연스럽게 비므로 (마지막 forward의 마커 ACK 후 큐 idle) 수정 48 부작용 자동 해결.

**I3 — 상태 변경 시점 제약 (no stale state)**
> robot의 휘발성 상태(`current_node`, `heading`, `carrying_shelf`)는 ACK 시점에만 변경.

- ACK 이전 robot 상태 = 마지막 확인된 ground truth.
- 예측값(turn 후 heading 등)은 큐 entry의 `expected_*` 필드에 보관, robot에 반영 안 함.
- 결과: 수정 43의 `_predict_heading_after_inflight` 같은 헬퍼 불필요. path 계산이 필요한 곳은 `queue.peek_expected_state()` 호출.

**I4 — ACK 일치성 (ACK matching)**
> 모든 cmd는 정확히 1개의 ACK를 가짐. ACK 수신 시 in_flight의 cmd 종류와 일치 확인.

- `forward` → 마커 메시지 (`marker.id == in_flight.target_node` 확인)
- `turn_left/right/180` → cmd_ack (cmd 문자열 일치)
- `lift_up/down` → cmd_ack (cmd 문자열 일치)
- 불일치 시: error 로그 + 큐 sync 시도(미정 — 1.3에서 정책 결정)

### 자료구조 (finalized)

```python
# server/command_queue.py (신규 모듈)
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

@dataclass
class CommandEntry:
    cmd: str                                # "forward" / "turn_*" / "lift_*"
    target_node: Optional[int] = None       # forward: 도착 예정 노드
    expected_heading: Optional[int] = None  # turn 후 예상 heading
    issued_at: float = 0.0                  # publish 시각 (디버깅/타임아웃용)

class CommandQueue:
    """AGV 1대의 명령 lifecycle 관리 (FIFO + in-flight 슬롯)."""

    def __init__(self, rid: int):
        self.rid = rid
        self.pending: deque[CommandEntry] = deque()
        self.in_flight: Optional[CommandEntry] = None

    def enqueue(self, entry: CommandEntry) -> None:
        self.pending.append(entry)

    def enqueue_many(self, entries: list[CommandEntry]) -> None:
        self.pending.extend(entries)

    def can_dispatch(self) -> bool:
        """발행 가능 (in_flight 없고 pending 있음)."""
        return self.in_flight is None and len(self.pending) > 0

    def dispatch(self) -> CommandEntry:
        """다음 cmd를 in_flight로 옮김. 호출 전 can_dispatch() 확인 필수."""
        entry = self.pending.popleft()
        self.in_flight = entry
        return entry

    def ack(self) -> Optional[CommandEntry]:
        """in_flight 완료 처리 후 반환."""
        entry = self.in_flight
        self.in_flight = None
        return entry

    def is_idle(self) -> bool:
        """완전 idle: 미발행 + in-flight 둘 다 없음. (수정 48 가드 대체)"""
        return self.in_flight is None and not self.pending

    def clear_pending(self) -> None:
        """인터셉트/replan 시 미발행 명령 폐기. in_flight는 유지 (AGV가 이미 받음)."""
        self.pending.clear()

    def peek_expected_node(self) -> Optional[int]:
        """다음 forward의 target_node (충돌 체크용). pending head 또는 in_flight."""
        if self.in_flight and self.in_flight.cmd == "forward":
            return self.in_flight.target_node
        if self.pending and self.pending[0].cmd == "forward":
            return self.pending[0].target_node
        return None
```

`RequestHandler` 상태:
```python
self.command_queues: Dict[int, CommandQueue] = {rid: CommandQueue(rid) for rid in robots}
```

### 9종 게이트 → 큐 시스템 매핑

| 기존 게이트 | 큐 시스템 대체 | 비고 |
|---|---|---|
| `_in_flight_cmds[rid]` | `queue.in_flight.cmd` | 1:1 매핑 |
| `_blocked_robots` | dispatch 가드(`can_dispatch` + 충돌 체크) 실패 시 큐에 남김 | 모든 ACK 시점에서 자동 retry → 별도 set 불필요 |
| `_reserved_nodes[node]` | 모든 robot의 `queue.peek_expected_node()` 합집합 | forward 충돌 체크가 큐 조회로 통합 |
| `_lifting_robots` | `queue.in_flight.cmd in ("lift_up", "lift_down")` | deadlock yield 가드는 in_flight cmd로 판단 |
| `planned_path` slide | 큐 길이로 대체. `queue.is_idle()` ⇔ "더 갈 곳 없음" | I2 invariant로 자동 정착 |
| `_yielded_staging_robots` | staging SM `State.YIELDED` | 카테고리 B 별도 SM (1.3 참조) |
| `_staged_to_ws` | staging SM `State.WAITING_FOR_NODE_ARRIVAL` | 동일 |
| `_goal_locked_robots` + `_deferred_goals` | `Dict[rid, goal]` 통합 | 카테고리 C, 큐와 독립 |
| 수정 48의 `not r.planned_path` | `queue.is_idle()` | I2가 정확히 대체 |

### Edge case 처리

**EC1 — 인터셉트 (path 재계획)**
- 게이트: `queue.in_flight is not None`이면 인터셉트 보류 (현재 `_in_flight_cmds`와 동일 가드).
- 재계획: ACK 후 `queue.clear_pending()` + 새 path entry들 `enqueue_many()`.
- → 의미적 게이트 `_in_flight_cmds` 가드를 큐 in_flight 검사로 단일화.

**EC2 — Deadlock yield**
- yield robot의 in_flight가 `lift_*`이면 yield 보류 (= 기존 `_lifting_robots` 가드).
- in_flight가 `forward/turn_*`이면 ACK 대기 후 `clear_pending()` + yield path enqueue.
- → 모든 가드가 `in_flight.cmd` 검사로 통합.

**EC3 — Forward 충돌 (target_node 다른 robot이 점유)**
- `dispatch` 직전에 `_check_forward_collision(entry)`:
  - 다른 robot의 `current_node == entry.target_node` → 보류
  - 다른 robot의 `queue.peek_expected_node() == entry.target_node` → 보류
- 보류 = 큐에서 빼지 않음. 어떤 ACK든 도착하면 dispatch 자동 재시도 (`_try_dispatch_all()`).
- → `_blocked_robots` 게이트 + `_retry_blocked_robots` 헬퍼 둘 다 불필요. 큐 자체가 retry 큐.

**EC4 — Staging 비동기 release (카테고리 B)**
- staging SM을 별도 정리 (1.3에서 상세). 큐와는 독립.
- release 시점에 staging SM이 robot의 큐에 `clear_pending() + enqueue_many(new_path)` 발화.

**EC5 — 큐 sync 실패 (I4 위반)**
- 마커 id가 in_flight.target_node와 불일치, 또는 cmd_ack의 cmd가 in_flight.cmd와 불일치.
- 정책 (잠정): error 로그 + 큐 통째로 clear + path replan. 빈도 모니터링 → 0에 가까워야 함.
- 더 정교한 정책은 1.3에서.

### 단일 발행점 보장 (I1 강제)

```python
# server/_movement_mixin.py
def _send_next_command(self, rid: int) -> bool:
    queue = self.command_queues[rid]
    if not queue.can_dispatch():
        return False
    next_entry = queue.pending[0]  # peek

    # forward 충돌 체크 (EC3)
    if next_entry.cmd == "forward":
        if not self._check_forward_safe(rid, next_entry.target_node):
            return False  # 보류, 큐에서 빼지 않음

    queue.dispatch()  # in_flight로 이동
    self.mqtt_publisher.publish_cmd(rid, next_entry.cmd)
    return True

def _try_dispatch_all(self) -> None:
    """모든 robot에 dispatch 시도 (ACK 받으면 호출)."""
    for rid in self.robot_manager.robots:
        self._send_next_command(rid)
```

### ACK 핸들러 통일

```python
# server/_marker_mixin.py (큐 기반 재작성)
def _handle_marker_report(self, data):
    rid, node = data["rid"], data["marker_id"]
    queue = self.command_queues[rid]
    entry = queue.ack()  # in_flight 완료
    
    # I4 일치성 체크
    if entry and entry.cmd == "forward" and entry.target_node != node:
        return self._handle_marker_mismatch(rid, entry, node)
    
    # I3: robot 상태 갱신은 ACK 후
    self.robot_manager.update_robot_position(rid, node)
    if "heading" in data:
        robot.heading = data["heading"]
    
    # ... (lookahead, staging, task 처리)
    
    self._try_dispatch_all()  # 모든 robot retry
    self._try_assign_pending_tasks()

def _handle_cmd_ack(self, data):
    rid, cmd = data["rid"], data["cmd"]
    queue = self.command_queues[rid]
    entry = queue.ack()
    
    if entry and entry.cmd != cmd:
        return self._handle_ack_mismatch(rid, entry, cmd)
    
    # I3: turn → heading 갱신, lift → carrying_shelf 처리
    if cmd in ("turn_left", "turn_right", "turn_180"):
        robot.heading = entry.expected_heading
    elif cmd in ("lift_up", "lift_down"):
        # 기존 _handle_pickup_ack / _handle_putdown_ack 로직
        ...
    
    self._try_dispatch_all()
```

---

## 1.3 마이그레이션 플랜

### 원칙

1. **점진적** — 한 게이트씩 제거. 매 단계 pytest 21 + 시뮬 4 시나리오 통과 확인.
2. **병행 모드** — 큐와 기존 게이트 동시 작동 (1~2단계). assert로 일치 검증.
3. **단계별 commit 분리** — 롤백 가능. 각 단계 메시지에 "REFACTOR E 단계 N.M" 명시.
4. **단일 발행점 강제** (I1) — 매 단계 끝에 `mqtt_publisher.publish_cmd` 직접 호출 grep으로 0건 확인.

### 단계 구조

| 단계 | 내용 | 검증 | 위험도 |
|---|---|---|---|
| **2.1** | `command_queue.py` 모듈 신규 + 클래스 | unit test (pytest 신규 추가) | 낮음 (병행 X, 사용 X) |
| **2.2** | `RequestHandler.__init__`에 큐 초기화 | pytest 21 | 낮음 |
| **2.3** | `_send_next_command` 큐 기반 재작성 (병행: 기존 게이트도 update) | pytest 21 + assert 일치 | 중 |
| **2.4** | 마커/cmd_ack 핸들러에 `queue.ack()` 추가 (병행) | pytest 21 + 시뮬 4 시나리오 + assert | 중 |
| **3.1** | `_reserved_nodes` 제거 → `queue.peek_expected_node` 합집합 | pytest + 시뮬 | 중 |
| **3.2** | `_in_flight_cmds` 제거 → `queue.in_flight.cmd` 참조 | pytest + 시뮬 | 중 (참조 위치 많음) |
| **3.3** | `_blocked_robots` + `_retry_blocked_robots` 제거 → `_try_dispatch_all` | pytest + 시뮬 | 높음 (대규모 변경) |
| **3.4** | `_lifting_robots` 제거 → in_flight.cmd 검사로 대체 | pytest + 시뮬 | 낮음 (단순 참조 대체) |
| **3.5** | `planned_path` invariant 정착 (마지막 도달 시 비움) — **시뮬 멈춤 버그 자연 해결** | 시뮬 (user1 5개 task 완주) | 낮음 |
| **4.1** | `_goal_locked_robots` + `_deferred_goals` → `Dict[rid, goal]` 통합 | pytest + 시뮬 (deadlock 시나리오) | 낮음 |
| **5.1** | `StagingStateMachine` 클래스 설계 + 신규 모듈 | unit test | 낮음 |
| **5.2** | `_yielded_staging_robots` + `_staged_to_ws` → SM 상태로 흡수 | 시뮬 (staging/포워딩 4 시나리오 전부) | **높음** |
| **6.1** | 미사용 헬퍼 제거 (`_predict_heading_after_inflight` 등) | pytest + 시뮬 | 낮음 |
| **6.2** | 시연 흐름 재검증 (user1+user2 동시, mqtt_test 자동 체인) | 시연 영상 | — |
| **6.3** | FLOWCHART.md 수정 50 기록 + 메모리 정리 | — | — |

### 병행 모드 패턴 (단계 2.3~2.4)

```python
def _send_next_command(self, rid: int) -> bool:
    queue = self.command_queues[rid]
    queue_can_dispatch = queue.can_dispatch()
    # ... 기존 로직 ...
    legacy_can_dispatch = (rid not in self._in_flight_cmds
                           and rid not in self._blocked_robots
                           and robot.command_queue)
    assert queue_can_dispatch == legacy_can_dispatch, \
        f"Queue/legacy mismatch for rid={rid}: queue={queue_can_dispatch}, legacy={legacy_can_dispatch}"
    # ... 기존 발행 + 큐도 dispatch ...
```

assert 실패 = 큐와 기존 동작 불일치 = 즉시 디버깅. 모든 시나리오에서 assert 통과하면 기존 게이트 제거 안전.

### 롤백 전략

- 각 단계는 git commit 1개. 문제 발견 시 `git revert <단계 commit>`로 즉시 복원.
- 단계 2.x (병행 모드)는 기존 동작 보존 → 롤백 영향 최소.
- 단계 3.x (게이트 제거)부터 의미 있는 변경 → 단계 끝 직후 시뮬 시연으로 검증.
- 단계 5.x (staging SM)는 가장 큰 변경 — 별도 브랜치(`refactor-e-staging`) 사용 고려.

### 회귀 시그널

각 단계 commit 전 통과해야 할 체크:

1. `pytest tests/` — 21 passed
2. `mqtt_test.py` 자동 체인 — 4 시나리오 (포워딩/인터셉트/staging/PICK차단) 통과
3. 시뮬 멈춤 버그 (현재 user1 5개 task 중 2개에서 정지) — 단계 3.5 이후 완주 확인
4. 시연 흐름 — user1 + user2 동시, deadlock 없음, 완주

### 작업량 추정

- 단계 2.1~2.4 (인프라 + 병행): ~0.5일
- 단계 3.1~3.5 (카테고리 A + D 제거): ~1일
- 단계 4.1 (카테고리 C): ~0.5일
- 단계 5.1~5.2 (카테고리 B + staging SM): ~1.5일
- 단계 6.x (정리/검증/문서): ~0.5일
- **합계: ~4일** (테스트 + 시연 검증 포함)

위험 요소:
- 단계 5.2 (staging SM)에서 회귀 발생 시 +1일
- 시연 도중 발견되는 신규 race 처리 +0.5~1일

### 세션 단절 대비

각 단계 commit message에 다음 형식 사용:
```
REFACTOR E 단계 N.M: <짧은 설명>

- 변경: ...
- 다음 단계: N.(M+1) <짧은 설명>
- 체크리스트: REFACTOR_E.md 상단 참조
```

새 세션 진입 시 순서:
1. `MEMORY.md` "다음 세션 즉시 처리" 항목 확인
2. `server/REFACTOR_E.md` 상단 진행 체크리스트 확인 → 마지막 [x] 다음 단계가 현재 위치
3. `git log --oneline | grep REFACTOR` → 단계별 commit 확인
4. 해당 단계 작업 재개

---

## 회귀 검증 자산

- **pytest**: `tests/` 5파일 21개 (현재 baseline)
- **시뮬 4 시나리오**: 포워딩/인터셉트/staging/PICK차단 ([[test_order_scenarios]] 메모 참조)
- **mqtt_test.py 자동 체인**: `시작` 명령으로 두 사용자 동시 시작

각 단계마다:
1. pytest 21 통과
2. 4 시나리오 시뮬 통과
3. 시연 흐름 (user 1 + user 2 동시) 통과

---

## 변경 내역 (단계별)

### 2.1 (2026-05-26) — `command_queue.py` 모듈 + 클래스
- 신규: `server/command_queue.py` — `CommandEntry` dataclass + `CommandQueue` 클래스
- 신규: `tests/test_command_queue.py` — 10개 단위 테스트 (idle/dispatch/ack cycle / I1 위반 raises / peek / clear_pending / 전 경로 시뮬)
- 사용처 없음 (단계 2.2에서 wire up)
- pytest: 31 passed (기존 21 + 신규 10)
- 영향: 0 (코드 호출 0)

### 2.2 (2026-05-26) — `RequestHandler.__init__` 큐 초기화
- `request_handler.py` import: `from .command_queue import CommandQueue`
- `__init__` 마지막 (브로드캐스트 콜백 직전)에 `self.command_queues: Dict[int, CommandQueue] = {rid: CommandQueue(rid) for rid in self.robot_manager.robots}` 추가
- pytest: 31 passed
- 영향: 0 (사용처 없음, 초기화만)

### 2.3 (2026-05-26) — `_send_next_command` 큐 병행 작성
- `_movement_mixin.py` import: `from .command_queue import CommandEntry`
- `_send_next_command` publish 직후에 큐 dispatch 동기화:
  - `next_cmd == "forward"` → `CommandEntry(cmd, target_node=next_node)`
  - `next_cmd in turn_*` → `expected_heading=(robot.heading + delta) % 360`
  - 그 외 → `CommandEntry(cmd)`
  - `queue.enqueue(entry) + queue.dispatch()` (in_flight 슬롯 채움)
- assert 2개:
  - `queue.in_flight.cmd == next_cmd` (I1 큐 단일성)
  - `(rid in _in_flight_cmds) == (queue.in_flight is not None)` (병행 동기 일치)
- 단계 2.4(ACK)와 한 쌍. ACK 호출 추가 전엔 큐가 영원히 in_flight 점유 상태 → 두 번째 cmd 발행 시 `RuntimeError` 발생 위험. **단계 2.4 미적용 시 실서버 작동 안 함** (단위 테스트 31개는 통과)
- pytest: 31 passed
- 위험: pytest 통과해도 실 시뮬에선 두 번째 dispatch에서 RuntimeError. 2.4와 묶어서 검증해야 함

### 2.4 (2026-05-26) — 마커/cmd_ack 핸들러 큐 ack
- `_marker_mixin.py:_handle_marker_report` — 기존 `_in_flight_cmds.pop` 직후 `queue.ack()` 호출. I4 위반 시 WARN 로그 (target_node ↔ marker_id 불일치)
- `_marker_mixin.py:_handle_cmd_ack` — 기존 `_in_flight_cmds.pop` 직후 `queue.ack()` 호출. cmd 불일치 시 WARN
- 이제 2.3에서 우려한 두 번째 dispatch RuntimeError 해결 (ACK 도착 시 큐 in_flight 비워짐)
- pytest: 31 passed
- **단계 2 완료**: 병행 모드 정상 작동. 시뮬 검증 시 assert 0건 / WARN 0건이어야 단계 3 진입 가능

### 2.x 시뮬 검증 (2026-05-26) — 사용자 시뮬 실행
- user1 주문 16 시뮬 — assert 0건 / WARN 0건 확인 ✓
- 멈춤 버그(수정 48 부작용)는 단계 3.5까지 그대로 (예상)
- → 단계 3 진입 안전

### 3.1 (2026-05-26) — `_reserved_nodes` 제거
- 신규 헬퍼: `_movement_mixin.py:_is_node_reserved_by(node, by_rid)` — `command_queues[by_rid].peek_expected_node() == node` 검사
- 참조 5곳 교체:
  - `_movement_mixin.py:106` (blocker 탐지)
  - `_movement_mixin.py:139-143` (yield 노드 reserved 체크)
  - `_movement_mixin.py:508` (forward 충돌 체크)
- 삭제:
  - `_movement_mixin.py:516` `self._reserved_nodes[next_node] = rid` (큐 dispatch가 대체)
  - `_marker_mixin.py:70-71` 마커 도착 시 reservation 해제 (큐 ack가 대체)
  - `request_handler.py:94` `self._reserved_nodes` 초기화 삭제
- `_clear_robot_reservation` 함수 본문 큐 기반으로 재작성 (`queue.clear_pending()`)
- docstring 갱신: `_movement_mixin.py`, `_marker_mixin.py` 상단의 self 상태 목록
- 테스트 갱신: `tests/test_collision.py` 4개 테스트가 `_reserved_nodes`를 직접 참조하던 부분 → `command_queues[rid].peek_expected_node()`로 교체
- pytest: 31 passed
- 영향: 카테고리 A 게이트 1/4 제거. forward 충돌 체크가 큐 단일 진실 원천 사용

### 3.2 (2026-05-26) — `_in_flight_cmds` 제거
- `_send_next_command` 재작성 (큐 단일 진실 원천):
  - in-flight 가드: `queue.in_flight is not None`로 교체
  - 충돌 체크 통과 후 큐 entry 생성 → `queue.dispatch()` → `publish_cmd` 순서
  - 기존 `_in_flight_cmds[rid] = next_cmd` 라인 + 단계 2.3의 assert 2개 모두 삭제
- `_predict_heading_after_inflight` 큐 기반 재작성: `queue.in_flight.expected_heading` 직접 사용 (CommandEntry가 보관)
- `_handle_marker_report` / `_handle_cmd_ack` 의 `_in_flight_cmds.pop` 삭제 (큐 ack가 대체)
- `_workflow_mixin.py:_should_intercept` 인터셉트 가드: `carrying_robot.rid in _in_flight_cmds` → `carrying_queue.in_flight is not None`
- `request_handler.py:102` `self._in_flight_cmds` 초기화 삭제
- pytest: 31 passed
- 영향: 카테고리 A 게이트 2/4 제거. cmd lifecycle 추적이 큐 in_flight으로 통합

### 3.3 (2026-05-26) — `_blocked_robots` + `_retry_blocked_robots` 제거
- 신규 헬퍼: `_movement_mixin.py:_is_blocked(rid)` — `command_queue 비어있지 않은데 queue.in_flight 없음` ⇔ blocked
- `_retry_blocked_robots` → `_try_dispatch_all` 이름 변경 + 본문 큐 기반 재작성:
  - 모든 robot 순회 (set 멤버십 아닌 큐 상태)
  - `_is_blocked(rid)` 인 robot에만 `_send_next_command` 재시도
  - deadlock 감지 조건의 `blocker_rid in _blocked_robots` → `_is_blocked(blocker_rid)`
  - 하위 호환 alias `_retry_blocked_robots = _try_dispatch_all` 유지 (단계 6.1에서 제거)
- 삭제:
  - `_send_next_command`의 `_blocked_robots.add/discard` 4곳
  - yield 코드의 `_blocked_robots.discard` 3곳 (Strategy 1/2)
  - `request_handler.py:82` `self._blocked_robots` 초기화
- `_marker_mixin.py` 5곳 `_retry_blocked_robots()` → `_try_dispatch_all()` 교체 + docstring 갱신
- 테스트 갱신:
  - `tests/test_collision.py` — `1 in handler._blocked_robots` → `handler._is_blocked(1)`, `_retry_blocked_robots` → `_try_dispatch_all`
  - `tests/test_deadlock.py` — 동일 패턴
- pytest: 31 passed
- 영향: 카테고리 A 게이트 3/4 제거. blocked 상태가 큐 상태로 추론 (별도 set 불필요)

### 3.4 (2026-05-26) — `_lifting_robots` 제거
- 신규 헬퍼: `_movement_mixin.py:_is_lifting(rid)` — `queue.in_flight.cmd in ("lift_up", "lift_down")`
- `_resolve_deadlock`의 `rid_a in _lifting_robots or rid_b in _lifting_robots` → `_is_lifting(rid_a) or _is_lifting(rid_b)`
- `_workflow_mixin.py` 3곳 — lift_up/lift_down 발행 패턴 통일:
  - 기존: `publish_cmd` 직접 호출 + `_lifting_robots.add` (I1 위반)
  - 신규: `robot.command_queue.append(cmd)` + `_send_next_command(rid)` (큐 경유, I1 준수)
- `_marker_mixin.py:_handle_cmd_ack` — `_lifting_robots.discard` 삭제 (큐 ack가 처리)
- `request_handler.py:98` `self._lifting_robots` 초기화 삭제
- pytest: 31 passed
- **카테고리 A (4종) 모두 제거 완료**: 의미적 게이트 기반 race 시리즈 종료. 의미 1줄 = `queue.in_flight` + `queue.peek_expected_node`

### 3.5 (2026-05-26) — `planned_path` invariant 정착 (시뮬 멈춤 버그 해결)
- `_marker_mixin.py:_handle_marker_report` path slide 직후 1줄 추가:
  ```python
  if len(robot.planned_path) <= 1:
      robot.planned_path = []
  ```
- 의미: "마지막 노드 도달 = path 완료 → 비움". 수정 48의 `not r.planned_path` 가드와 의미 일치.
- 효과: parking 종료 시 path 자동 비움 → `get_available_robot` 가용 분류 정상화 → pending dispatch 가능
- 시뮬 멈춤 버그(user1 주문이 task 2/N에서 정지)는 이제 자연 해결. 시뮬 검증 필요.
- pytest: 31 passed
- **카테고리 D 정착**. 단계 3 전체 완료 — 카테고리 A + D 모두 정리됨

### 4.1 (2026-05-26) — `_goal_locked_robots` + `_deferred_goals` 통합
- `_goal_locked_robots: Set[int]` 삭제. `_deferred_goals: Dict[int, int]` 단일 진실 원천. 멤버십 = 키 존재.
- `request_handler.py:89` set 초기화 삭제. 주석에 REFACTOR E 4.1 명시.
- `_movement_mixin.py:_check_goal_locked_robots` — `for rid in list(_goal_locked_robots)` → `for rid, goal in list(_deferred_goals.items())`. `.discard` 호출 3건 모두 삭제, `.pop` 만 남김.
- `_movement_mixin.py:_resolve_deadlock` goal-block 분기 — `yield_rid in _goal_locked_robots` → `yield_rid in _deferred_goals`. `.add` 호출 삭제 (`_deferred_goals[yield_rid] = goal` 한 줄로 의미 일치).
- `DISPATCH_FLOW.md` 상태 변수 표 갱신 — `_reserved_nodes` / `_blocked_robots` / `_in_flight_cmds` / `_goal_locked_robots` 행 삭제, `command_queues` / `_deferred_goals` 행 추가.
- 함수명 `_check_goal_locked_robots`는 의미적 정확 (목적 변화 없음) → 유지.
- pytest: 31 passed
- **카테고리 C 정착**. 단계 4 완료 — 게이트 9종 중 7종 제거 (A 4종 + C 2종 + D 1종 통합). 남은 = B 2종 (`_yielded_staging_robots`, `_staged_to_ws`) → 단계 5.

### 3.x 시뮬 검증 (2026-05-26 부분, 미완)
- user1 주문 26, 28 정상 완주 — 이전 멈춤 패턴(2/N 정지) 사라짐 ✓
- WARN 0건 / assert 0건 — 큐 ack 정상 ✓
- 단계 3.5 path slide invariant 정상 동작 ✓
- **잔여 의심**: 주문 28 완료 시 `task_statuses=['completed', 'completed', 'completed', 'in_progress']`
  - 1개 task가 in_progress인데 `order_complete` 받음 = GUI에서 미완료 클릭 가능성
  - 그 후 user1 주문 29~31이 모두 stock 부족으로 GUI 거부 → AGV idle (정상일 가능성)
  - **다음 세션에서 추가 분석 필요**: 진짜 멈춤인지 / 단순 후속 주문 없음인지
- **GUI 버그 (협업자)**: 같은 선반에 여러 품목(예: 1-4 캐러멜+퍼지) 시 1번 클릭만으로 `shelf_complete` 발송됨. 전체 품목 눌러야 발송되어야 정상. `warehouse_gui.py` enable_cells/shelf_complete 발화 조건 확인 필요

---

## 관련 메모/이력
- 수정 30 — 의미적 게이트 누적 시작점 (idle 주차지 + goal-lock 도입)
- 수정 46/46.1 — `_lifting_robots`, `_in_flight_cmds` 도입 (in-flight race)
- 수정 48 — `not planned_path` 가드 (현재 시뮬 멈춤 버그의 직접 원인)
- 수정 49 — stock race 옵션 A 정통 (이번 리팩토링과 무관, 별도 race 카테고리)
