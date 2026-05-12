# AGV 서버 설계 문서 (v5)

## 빠른 시작

### 1. 의존성 설치
```bash
pip install -r requirements.txt
```

### 2. MQTT 브로커 설치 (Mosquitto)
```bash
sudo apt update
sudo apt install mosquitto mosquitto-clients
sudo systemctl start mosquitto
sudo systemctl enable mosquitto
```

### 3. 실행 (터미널 3개)
```bash
# 터미널 1: 서버
cd TU_Capstone_Design
python3 -m server.main

# 터미널 2: Isaac Sim (현재 메인)
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step6_visual.py

# 터미널 3: CLI 테스트
python3 mqtt_test.py
```

> Webots 시뮬레이터는 레거시. 신규 개발은 Isaac Sim 기반.

### 4. 테스트 실행
```bash
cd TU_Capstone_Design
pytest                          # 전체 (21 passed)
pytest -m "stg or deadlock" -v  # 영역별
```

---

## 1. 전체 시스템 구조

```
+---------------+    MQTT          +-------------+      MQTT       +------------------+
|   mqtt_test   | ---------------> |   Server    | --------------> |  AGV (Isaac/RPi) |
|  (CLI 테스트) |  agv/algorithm   |  (이 서버)  |   /agv/cmd      |  Bridge + AGV    |
+---------------+                  +-------------+                 +---------+--------+
                                          ^                                  |
                                          |       /agv/marker                |
                                          |       /agv/cmd_ack               |
                                          +----------------------------------+
                                          |
                                          +- request_handler : 주문/명령/충돌/교착 처리 (핵심)
                                          +- task_manager    : 작업 분해/스케줄링
                                          +- order_optimizer : Nearest Neighbor 최적화
                                          +- shelf_manager   : 선반 상태 추적
                                          +- staging_manager : 작업대 회랑 게이팅 (STG)
                                          +- path_planner    : A* (회전 페널티 + 예약 기반)
                                          +- robot_manager   : 로봇 6단계 상태 머신
                                          +- db_loader       : 엑셀 DB 로더
```

**데이터 흐름 (cmd-based):**
1. CLI/GUI에서 `start_order` 발행 (`agv/algorithm` 토픽)
2. DB에서 물품 목록 로드 → 물품→선반 매핑 → Nearest Neighbor 방문 순서 최적화
3. 유휴 로봇에 작업 배정 → A* 경로 계획 → 노드 경로 추출
4. 노드 경로를 cmd로 분해: `forward` / `turn_left` / `turn_right` / `turn_180` 발행 (`/agv/cmd`)
5. AGV가 ArUco 마커 감지 → `/agv/marker` 발행 (마커 ID = 노드 ID) → 서버 위치 갱신 + 충돌 체크 후 다음 명령
6. 선반 노드 도착 → 서버 `lift_up` 발행 → AGV 리프트 상승 → `/agv/cmd_ack` (lift_up done)
7. 작업대로 cmd 시퀀스 연속 발행 → AGV 배달
8. GUI `완료 1` → `shelf_complete` 발행 → 선반 반납 또는 포워딩

## 2. 모듈별 역할

### 파일 구조
```
server/
+-- __init__.py          # 패키지 초기화
+-- config.py            # 설정값 관리
+-- main.py              # 서버 시작점 (WebSocket + MQTT 서버, 토픽 구독)
+-- request_handler.py   # 195줄 베이스: __init__, handle_message 라우터, 상태 조회
+-- _movement_mixin.py   # ★ 이동 명령 발행 + 충돌/교착 회피 + 경로 계획 (yield 로직)
+-- _marker_mixin.py     # AGV 이벤트 (marker, cmd_ack, marker_trigger)
+-- _workflow_mixin.py   # 주문/태스크/F-노드/인터셉트 워크플로우
+-- task_manager.py      # 작업 분해 (서브태스크 시퀀스 생성)
+-- order_optimizer.py   # Nearest Neighbor 선반 방문 순서 최적화 (구 task_scheduler)
+-- robot_manager.py     # 로봇 6단계 상태 머신 + apply_turn
+-- shelf_manager.py     # 선반 상태 관리 (IN_PLACE / CARRIED / AT_WORKSTATION)
+-- staging_manager.py   # 작업대 회랑 게이팅 (STG / TRG / 위치 기반 해제)
+-- path_planner.py      # A* 시간 기반 경로 계획 + calc_heading_from_path
+-- mqtt_client.py       # MQTT 클라이언트 publish + subscribe (구 mqtt_publisher)
+-- websocket_handler.py # WebSocket 서버 (Admin UI용, 선택적)
+-- db_loader.py         # 엑셀 주문 DB 로더 (pandas)
+-- map.json             # 8x6 그리드 맵 (48노드)
+-- shelf_config.json    # 선반 + 작업대 설정 (8개 선반 + 2개 WS)
+-- robot_config.json    # 로봇 설정 (AGV-1 home=9(W2), AGV-2 home=33(W1))
+-- requirements.txt     # 의존성 (paho-mqtt, websockets, pandas, openpyxl)
+-- Database/            # 주문 엑셀 데이터
```

### 각 모듈 설명

#### `config.py` — 설정 관리
```
- MQTT 호스트/포트: localhost:1883
- WebSocket 포트: 8765
- 맵 파일: server/map.json (8x6 그리드 + 작업대 2개, 48노드)
- 선반 설정: server/shelf_config.json
- 로봇 설정: server/robot_config.json
- MQTT 토픽: /agv/cmd, /agv/marker, /agv/cmd_ack, agv/algorithm
```

#### `shelf_manager.py` — 선반 관리
```
상태:
  IN_PLACE       : 선반 홈 위치에 정치
  CARRIED        : AGV가 운반 중 (이동 중)
  AT_WORKSTATION : 작업대에 도착, 피킹 대기 중

상태 전환:
  WS 도착(DELIVER_TO_WS) → mark_shelf_at_workstation() → AT_WORKSTATION
  WS 출발(shelf_complete) → mark_shelf_picked_up()     → CARRIED
  홈 putdown 완료         → mark_shelf_returned()       → IN_PLACE
```

#### `staging_manager.py` — 작업대 회랑 게이팅
```
STG 판단:    should_stage(ws_node, rid) → None(즉시 진입) / staging_node(우회)
TRG 해제:    handle_marker_trigger(rid, marker_id) → 큐 다음 AGV release
위치 기반:   check_position_release(rid, node) → is_exiting 로봇이
            corridor_area({ws_node, gateway_node}) 밖으로 나가면 즉시 해제
포워딩 해제: release_corridor_without_trigger(ws_node, rid) → 즉시 해제
```

> **수정 28 (2026-05-12):** `staging_node`를 corridor 진입 경로 밖으로 분리.
> W1: staging 25→41 / W2: staging 17→1. gateway는 그대로(25, 17).
> "대기자가 입구를 막는" deadlock 근본 차단.

#### `task_manager.py` — 작업 관리
```
서브태스크 타입:
  GO_TO_SHELF    : 선반으로 이동
  PICKUP_SHELF   : 리프트 상승
  DELIVER_TO_WS  : 작업대로 배달
  WAIT_PICKING   : 피킹 대기 (GUI shelf_complete 대기)
  RETURN_SHELF   : 선반 원위치 복귀
  FORWARD_SHELF  : 다른 작업대로 포워딩

주요 메서드:
  rotate_shelf_to_end(task_id)          : 블록 선반 서브태스크를 맨 뒤로 (Bug A)
  handle_shelf_complete(task_id)        : WAIT_PICKING 완료 처리 (선반 단위)
  find_task_waiting_for_shelf(shelf_id) : 해당 선반의 WAIT_PICKING 태스크 탐색
  insert_forward_return_subtasks(...)   : 포워딩 후 재픽업 사이클 삽입
```

#### `robot_manager.py` — 로봇 관리
```
상태 머신 (6단계):
  IDLE            : 대기
  MOVING_TO_SHELF : 선반으로 이동 중
  PICKING_UP_SHELF: 리프트 상승 중
  DELIVERING_TO_WS: 작업대로 배달 중
  WAITING_FOR_PICK: 피킹 대기
  RETURNING_SHELF : 선반 복귀 중
```

#### `path_planner.py` — 경로 계획
```
- map.json 로드 (노드 타입 포함)
- astar_with_time(): 시간 기반 A* (예약 기반 충돌 회피)
  - turn_penalty=0.3: 방향 전환 시 추가 비용 (회전 최소화)
  - start_heading: 현재 진행 방향 전달 → 직진 경로 우선 선택
  - state: (node, time, dir) — dir=-1(미정) / 0=N / 1=E / 2=S / 3=W
- 선반 노드 통과 제외 (출발/도착만 허용)
- compress_to_node_path(): 시간 차원 제거 후 노드 시퀀스 추출
```

#### `request_handler.py` — 요청 처리 (핵심 알고리즘)
```
주요 핸들러:
  _handle_start_order()      : 주문 시작 (DB 로드 → 선반 배정 → 명령 발행)
  _handle_shelf_complete()   : 선반 피킹 완료 → return/forward 결정
  _handle_order_complete()   : 주문 완료 기록
  _handle_marker_report()    : /agv/marker 수신 → 위치 갱신 → 다음 명령 발행
  _handle_cmd_ack()          : /agv/cmd_ack 수신 → turn/lift 완료 처리
  _handle_putdown_ack()      : lift_down 완료 → 다음 선반 or IDLE

핵심 내부 메서드:
  _send_next_command(rid)        : 다음 forward/turn/lift 명령 결정 + 발행
                                   - forward 시 next_node 충돌 체크 → _reserved_nodes 예약
  _retry_blocked_robots()        : blocked 로봇 재시도 + 교착 감지
  _get_blocker_of(rid)           : rid를 막는 다른 로봇 rid 반환
  _is_staging_robot(rid)         : staging 큐 멤버십 검사 (수정 28)
  _resolve_deadlock(a, b)        : 교착 해제
                                   우선순위: staging > carrying > rid (수정 28)
                                   전략1: excluded_transit A*로 우회 경로
                                   전략2: yield 로봇이 옆 노드로 이동 후 재계획
                                   staging-yield: 1-step만 이동 (재계획 안 함)
  _find_yield_node(rid, contested): 인접 비-선반 비-점유 노드 탐색
  _replan_for_placed_shelf(node) : 선반 반납 후 해당 노드 경유 운반 로봇 재계획
  _try_assign_pending_tasks()    : PENDING 작업 재배정 루프 (F 노드 6분기 포함)
  _get_shelf_availability()      : 선반 가용성 판단 ('go'/'direct'/'pending')
  _try_intercept_returning_shelf(): Node U — 복귀 중 인터셉트
  _plan_and_publish_move()       : STG 체크 포함 경로 계획 (cmd 발행은 _send_next_command)
```

---

## 3. 맵 구조 (8×6 + 작업대 2개)

```
        Col1    Col2    Col3    Col4    Col5    Col6    Col7    Col8
        +-------+-------+-------+-------+-------+-------+-------+-------+
Row 1   |   1   |   2   |   3   |   4   |   5   |   6   |   7   |   8   |     STG(1) = W2 staging (수정 28)
        +-------+-------+-------+-------+-------+-------+-------+-------+
Row 2   | W2(9) |TRG(10)|  11   |  12   |  13   |  14   |  15   |  16   |
        +-------+-------+-------+-------+-------+-------+-------+-------+
Row 3   |  17   |  18   |[S1-1] |[S1-2] |  21   |[S1-3] |[S1-4] |  24   |     gateway(17) = W2
        +-------+-------+-------+-------+-------+-------+-------+-------+
Row 4   |  25   |  26   |[S2-1] |[S2-2] |  29   |[S2-3] |[S2-4] |  32   |     gateway(25) = W1
        +-------+-------+-------+-------+-------+-------+-------+-------+
Row 5   |W1(33) |TRG(34)|  35   |  36   |  37   |  38   |  39   |  40   |
        +-------+-------+-------+-------+-------+-------+-------+-------+
Row 6   |  41   |  42   |  43   |  44   |  45   |  46   |  47   |  48   |     STG(41) = W1 staging (수정 28)
        +-------+-------+-------+-------+-------+-------+-------+-------+
```

- **총 노드**: 48개 (8 × 6 그리드)
- **S (선반)**: 8개 — 19, 20, 22, 23 (Row 3) / 27, 28, 30, 31 (Row 4)
- **W (작업대)**: 2개 — W1=33 (Row 5 Col 1) / W2=9 (Row 2 Col 1)
- **W1(33)**: gateway=25, staging=41, trigger=34
- **W2(9)**:  gateway=17, staging=1,  trigger=10
- **AGV 홈**: AGV-1 home=9(W2) / AGV-2 home=33(W1)

> 노드 = (row-1) × 8 + col

---

## 4. 통신 프로토콜

### MQTT 토픽 전체 목록

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/cmd` | Server → AGV | 이동/회전/리프트 명령 (cmd-based) |
| `/agv/marker` | AGV → Server | ArUco 마커 감지 (마커 ID + heading) |
| `/agv/cmd_ack` | AGV → Server | 회전/리프트 완료 알림 |
| `agv/algorithm` | GUI/CLI → Server | UI 명령 수신 (주문/완료) |
| `warehouse/agv/at_ws` | Server → GUI | AGV 작업대 도착 알림 (선반 ID 포함) |

### 서버 → AGV 명령 (`/agv/cmd`)

```json
{"rid": 1, "cmd": "forward"}
{"rid": 1, "cmd": "turn_left"}
{"rid": 1, "cmd": "turn_right"}
{"rid": 1, "cmd": "turn_180"}
{"rid": 1, "cmd": "lift_up"}
{"rid": 1, "cmd": "lift_down"}
```

- `forward`: 현재 heading 방향으로 한 칸 직진 (마커 감지 시 자동 종료)
- `turn_*`: 제자리 회전 (완료 시 `cmd_ack` 발행)
- `lift_up/down`: 선반 리프트 (완료 시 `cmd_ack` 발행)

### AGV → 서버 위치 보고 (`/agv/marker`)

```json
{"rid": 1, "marker_id": 14, "heading": 90, "ts": 1700000000}
```

- `heading`: 서버 기준 (0=North, 90=East, 180=South, 270=West)
- 마커 ID = 노드 ID (ArUco 마커 번호 = 그리드 노드 번호)

### AGV → 서버 완료 알림 (`/agv/cmd_ack`)

```json
{"type": "cmd_ack", "rid": 1, "cmd": "turn_left", "status": "done"}
{"type": "cmd_ack", "rid": 1, "cmd": "lift_up",   "status": "done"}
```

### API (agv/algorithm 토픽)

**주문 시작:**
```json
{"type": "start_order", "사용자ID": 1, "주문번호": 1}
```

**선반 피킹 완료:**
```json
{"type": "shelf_complete", "사용자ID": 1}
// 선반번호 불필요 — 서버가 WS에서 AT_WORKSTATION 선반 자동 탐색
```

**주문 완료:**
```json
{"type": "order_complete", "사용자ID": 1, "주문번호": 1}
```

---

## 5. 로봇 상태 머신

```
IDLE -> MOVING_TO_SHELF -> PICKING_UP_SHELF -> DELIVERING_TO_WS -> WAITING_FOR_PICK
                                                                          |
                                              +---------------------------+
                                              |                           |
                                       [다른 작업대 필요]            [더 이상 불필요]
                                              |                           |
                                       FORWARD_SHELF              RETURNING_SHELF
                                       (다른 작업대로)             (선반 홈으로)
                                              |                           |
                                       WAITING_FOR_PICK          [다음 선반 있음?]
                                              |                    Yes -> MOVING_TO_SHELF
                                              |                    No  -> IDLE
                                              +---------------------------+
```

### cmd_ack에 의한 상태 전이

1. **GO_TO_SHELF 도착** (마커 감지) → `lift_up` 명령 → AGV 리프트 올림
2. **`cmd_ack: lift_up` 수신** → DELIVERING_TO_WS 상태, 작업대로 명령 시퀀스 발행 (STG 체크)
3. **RETURN/FORWARD 도착** (마커 감지) → `lift_down` 명령 → AGV 리프트 내림
4. **`cmd_ack: lift_down` 수신** → 다음 선반 or IDLE

### cmd-based 이동 흐름

```
서버: A* 경로 계획 → [node1, node2, node3, ...]
  ↓
서버: 현재 heading 확인 → 필요 시 turn 명령 발행
  ↓ (cmd_ack: turn done)
서버: forward 발행 + _reserved_nodes[next_node] = rid 예약
  ↓ (AGV 이동 → 마커 감지)
AGV: /agv/marker 발행 (marker_id = 도착 노드)
  ↓
서버: 위치 갱신 + _reserved_nodes 해제 + 다음 명령 결정
```

---

## 6. 핵심 알고리즘 포인트

### 충돌 회피 — 노드 예약 + Blocked

```python
_reserved_nodes: Dict[int, int]  # node_id → rid (forward 명령 시 목적지 예약)
_blocked_robots: Set[int]        # 충돌 예상으로 명령 보류 중인 로봇
```

`_send_next_command(rid)` — forward 명령 전 충돌 체크:
1. next_node is None → blocked 추가 (진행 방향에 노드 없음)
2. other.current_node == next_node → blocked 추가 (점유)
3. _reserved_nodes[next_node] == other_rid → blocked 추가 (예약됨)
4. 안전 → `_reserved_nodes[next_node] = rid` 예약 후 forward 발행

마커 도착 시: `_reserved_nodes` 해제 + `_retry_blocked_robots()` 호출
turn cmd_ack 완료 시: `_send_next_command(rid)` + `_retry_blocked_robots()` 호출 ← **교착 해제 트리거**

### 교착 감지 및 해제 (`_resolve_deadlock`)

```
_retry_blocked_robots():
  for rid in blocked_robots:
    success = _send_next_command(rid)
    if not success:
      blocker = _get_blocker_of(rid)
      if blocker in blocked_robots OR _is_staging_robot(blocker):  ← 수정 28: staging도 포함
        _resolve_deadlock(rid, blocker)

_resolve_deadlock(rid_a, rid_b):
  우선순위 결정:
    1. staging > 비-staging (수정 28: 대기 중인 AGV는 항상 yield)
    2. carrying(1) > non-carrying(0)
    3. 같으면 max(rid)가 yield
  staging AGV: yield_node로 1-step 이동 (재계획 안 함, _yielded_staging_robots에 등록)
  일반 AGV — 전략 1: A*(excluded_transit={blocker_node} + 점유선반) 우회 경로
            전략 2: yield 로봇이 _find_yield_node()로 옆 칸 이동 후 재계획
```

`_find_yield_node(rid, contested_node)`:
- contested_node 방향 이웃 제외
- `shelf_manager.all_shelf_nodes` 기준 선반 노드 항상 제외
- 타 로봇 점유/예약 노드 제외

### 선반 반납 후 재계획 (`_replan_for_placed_shelf`)

선반이 노드 X에 반납되면, 경로 중간에 X를 경유하는 운반 중 로봇이 있으면 재계획.
이유: 경로 계획 시 empty였던 노드에 선반이 나중에 놓임 → 통과 불가.

### F 노드 — 선반 가용성 6분기 (`_get_shelf_availability`)

| 상태 | 조건 | 반환 |
|------|------|------|
| IN_PLACE | 예약 없음 | `'go'` → 선반 홈으로 이동 |
| IN_PLACE | 다른 AGV GO_TO_SHELF 중 | `'pending'` → 순서 회전 시도 |
| CARRIED | 이동 중 | `'pending'` → 순서 회전 시도 |
| AT_WORKSTATION | carrier WAITING_FOR_PICK 중 | `'pending'` |
| AT_WORKSTATION | WS 회랑 점유 중 | `'pending'` |
| AT_WORKSTATION | 진입 가능 | `'direct'` → WS로 직행 |

### A* 회전 최소화 (turn_penalty)

```python
astar_with_time(turn_penalty=0.3, start_heading=robot.heading)
# state: (node, time, dir)
# 방향 전환 시 cost += 0.3
# start_heading 전달 → 현재 진행 방향 연속 우선 → 불필요한 회전 줄임
```

### STG — 작업대 회랑 게이팅

- `_plan_and_publish_move()` 내에서 `should_stage()` 자동 호출
- ⚠️ staging 체크는 `start==goal` 즉시도착 처리보다 **반드시 먼저** 실행 (Bug B 교훈)
- 스테이징 중 회랑 해제 시 `_retry_blocked_robots()` → 대기 로봇 자동 재시도
- 수정 28: staging 노드를 corridor 진입 경로 밖(W1=41, W2=1)으로 분리 → 점유자와 대기자가 같은 노드를 두고 충돌 불가

### TRG — 마커 트리거 + 위치 기반 해제

- ArUco 마커 통과 → `/agv/marker` 발행 → `handle_marker_trigger()` → 대기 AGV 해제
- 위치 기반: `is_exiting=True` 로봇이 회랑 구역({ws, gateway}) 밖으로 나가면 자동 해제

### Node U — 복귀 중 인터셉트

- `_try_intercept_returning_shelf()`: RETURNING_SHELF 로봇을 새 WS로 우회
- 인터셉트 시 기존 회랑 자동 해제 (`release_corridor_without_trigger`)

---

## 7. 의존성

```
websockets>=10.0    # WebSocket 서버 (Admin UI용)
paho-mqtt>=1.6.0    # MQTT 클라이언트
pandas              # 엑셀 파일 읽기 (db_loader)
openpyxl            # pandas의 xlsx 엔진
```

설치:
```bash
pip install -r requirements.txt
```

테스트:
```
pytest>=7.0         # pytest 회귀 테스트 (선택, tests/ 디렉토리)
```

---

## 8. 설정 파일

> 설정 JSON 파일은 `server/` 폴더에 위치 (Python 모듈과 동급)

### `robot_config.json`
```json
{
  "robots": {
    "1": {"name": "AGV-1", "home_node": 9},
    "2": {"name": "AGV-2", "home_node": 33}
  }
}
```

> AGV-1 홈=W2(9), AGV-2 홈=W1(33). 이전(AGV-1=33, AGV-2=9)에서 스왑 — 교착 회피.

### `shelf_config.json`
```json
{
  "shelves": {
    "19": {"label": "1-1", "items": [...]},
    "20": {"label": "1-2", "items": [...]},
    "22": {"label": "1-3", "items": [...]},
    "23": {"label": "1-4", "items": [...]},
    "27": {"label": "2-1", "items": [...]},
    "28": {"label": "2-2", "items": [...]},
    "30": {"label": "2-3", "items": [...]},
    "31": {"label": "2-4", "items": [...]}
  },
  "workstations": {
    "33": {"label": "W1", "gateway_node": 25, "staging_node": 41, "trigger_node": 34, "user_id": 1},
    "9":  {"label": "W2", "gateway_node": 17, "staging_node": 1,  "trigger_node": 10, "user_id": 2}
  },
  "shelf_node_map": {
    "1-1": 19, "1-2": 20, "1-3": 22, "1-4": 23,
    "2-1": 27, "2-2": 28, "2-3": 30, "2-4": 31
  }
}
```

> staging_node 변경 이력: 25→41 (W1), 17→1 (W2) — 수정 28 (2026-05-12)

### `map.json`
- 48개 노드 (8×6 그리드)
- 노드 타입: M (통로), S (선반), W (작업대)
- 양방향 엣지, cost = 1
- 좌표: x=col-0.5, y=(row-1)+0.5 (메모: 시각화 시 y 축 반전 가능)

---

## 9. 검증 / 테스트

### pytest 회귀 (21 passed)

```bash
pytest tests/                       # 전체
pytest tests/ -m stg                # 스테이징만
pytest tests/ -m deadlock           # 교착만
pytest tests/test_deadlock.py -v    # 파일 단위
```

테스트 파일:
- `tests/test_smoke.py` — 픽스처 sanity (5)
- `tests/test_collision.py` — 충돌 회피 (4)
- `tests/test_deadlock.py` — 교착 회피 (3) — `test_staging_blocker_forces_yield` 포함
- `tests/test_intercept.py` — Node U 인터셉트 (2)
- `tests/test_stg.py` — STG 게이팅 (7)

### 정적 분석

[`../검증_체크리스트.md`](../검증_체크리스트.md) — 충돌/교착/인터셉트/STG 4개 영역에 대한 정적 분석 + 의심/NG 항목 추적
