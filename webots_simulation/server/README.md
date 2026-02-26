# AGV 서버 설계 문서 (v4)

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
cd webots_simulation
python3 -m server.main

# 터미널 2: Webots 시뮬레이션
webots worlds/warehouse_4x8.wbt

# 터미널 3: CLI 테스트
python3 mqtt_test.py
```

---

## 1. 전체 시스템 구조

```
┌─────────────┐    MQTT          ┌─────────────┐      MQTT       ┌─────────────┐
│  mqtt_test   │ ──────────────>│   Server    │ ───────────────>│ AGV (Webots)│
│  (CLI 테스트) │  agv/algorithm  │  (이 서버)   │   /agv/plan     │  Supervisor │
└─────────────┘                 └─────────────┘   /agv/shelf_cmd └──────┬──────┘
                                       │           /agv/control          │
                                       │                                 │ /agv/arrived
                                       │                                 │ /agv/shelf_ack
                                       │                                 │ /agv/marker
                                       │<────────────────────────────────┘
                                       │
                                       ├─ request_handler : 주문/도착/리프트 처리 (핵심)
                                       ├─ task_manager    : 작업 분해/스케줄링
                                       ├─ task_scheduler  : Nearest Neighbor 최적화
                                       ├─ shelf_manager   : 선반 상태 추적
                                       ├─ staging_manager : 작업대 회랑 게이팅 (STG)
                                       ├─ path_planner    : A* (시간 기반 충돌 회피)
                                       ├─ robot_manager   : 로봇 6단계 상태 머신
                                       └─ db_loader       : 엑셀 DB 로더
```

**데이터 흐름:**
1. CLI(mqtt_test.py)에서 `start_order` 발행 (`agv/algorithm` 토픽)
2. DB에서 물품 목록 로드 → 물품→선반 매핑 → Nearest Neighbor 방문 순서 최적화
3. 유휴 로봇에 작업 배정 → A* 경로 계획
4. MQTT `/agv/plan`으로 경로 전송 → AGV 주행
5. AGV 중간 노드 도착 → `robot_position` 보고 → 서버 `resume` 명령
6. AGV 최종 도착 (`/agv/arrived`) → 선반 리프트 명령 (`/agv/shelf_cmd`)
7. AGV 리프트 완료 (`/agv/shelf_ack`) → 작업대로 배달
8. CLI `완료 1` → `shelf_complete` 발행 → 선반 반납 또는 포워딩


## 2. 모듈별 역할

### 파일 구조
```
server/
├── __init__.py          # 패키지 초기화
├── config.py            # 설정값 관리
├── main.py              # 서버 시작점 (WebSocket + MQTT 서버, 토픽 구독)
├── request_handler.py   # ★ 핵심: 요청 처리 + 로봇 배정 알고리즘
├── task_manager.py      # 작업 분해 및 스케줄링 (서브태스크 시퀀스 생성)
├── task_scheduler.py    # Nearest Neighbor 선반 방문 순서 최적화
├── robot_manager.py     # 로봇 6단계 상태 머신
├── shelf_manager.py     # 선반 상태 관리 (IN_PLACE / CARRIED / AT_WORKSTATION)
├── staging_manager.py   # 작업대 회랑 게이팅 (STG / TRG / 위치 기반 해제)
├── path_planner.py      # A* 시간 기반 경로 계획 (예약 기반 충돌 회피)
├── mqtt_publisher.py    # MQTT 발행 (/agv/plan, /agv/shelf_cmd, /agv/control)
├── websocket_handler.py # WebSocket 서버 (Admin UI용, 선택적)
├── db_loader.py         # 엑셀 주문 DB 로더 (pandas)
└── requirements.txt     # 의존성 (paho-mqtt, websockets, pandas, openpyxl)
```

### 각 모듈 설명

#### `config.py` — 설정 관리
```
- MQTT 호스트/포트: localhost:1883
- WebSocket 포트: 8765
- 맵 파일: config/map.json (8×4 그리드 + 작업대 2개)
- 선반 설정: config/shelf_config.json
- 로봇 설정: config/robot_config.json
- MQTT 토픽: /agv/plan, /agv/shelf_cmd, /agv/arrived, /agv/shelf_ack,
             /agv/marker, agv/algorithm, /agv/control
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
STG 판단: should_stage(ws_node, rid) → True(대기) / False(진입)
TRG 해제: handle_marker_trigger(marker_id) → 대기 AGV 해제
위치 기반 해제: check_position_release(rid, node) → is_exiting 로봇이
              회랑 구역({ws_node, gateway_node}) 밖으로 나가면 즉시 해제
포워딩 해제: release_corridor_without_trigger(ws_node, rid) → 즉시 해제
```

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
- 선반 노드 통과 제외 (출발/도착만 허용)
- 이동 로봇 예약에 +1 타임스텝 버퍼 (회전 지연 보정)
- 목표 대기 예약: 5스텝
```

#### `request_handler.py` — 요청 처리 (핵심 알고리즘)
```
주요 핸들러:
  _handle_start_order()      : 주문 시작 (DB 로드 → 선반 배정 → 경로 발행)
  _handle_shelf_complete()   : 선반 피킹 완료 → return/forward 결정
  _handle_order_complete()   : 주문 완료 기록
  _handle_robot_arrived()    : 최종 도착 → 다음 서브태스크 실행
  _handle_robot_position()   : 중간 노드 위치 갱신 → resume 판단 → 회랑 해제
  _handle_pickup_ack()       : 리프트 완료 → 작업대로 이동 명령
  _handle_putdown_ack()      : 내려놓기 완료 → 다음 선반 or IDLE
  _handle_mqtt_marker()      : ArUco 마커 → 회랑 해제

핵심 내부 메서드:
  _try_assign_pending_tasks(): PENDING 작업 재배정 루프 (F 노드 6분기 포함)
  _get_shelf_availability()  : 선반 가용성 판단 ('go'/'direct'/'pending')
  _try_intercept_returning_shelf(): Node U — 복귀 중 인터셉트
  _plan_and_publish_move()   : STG 체크 포함 경로 계획 + MQTT 발행
  _try_resume_waiting_robots(): NODE_WAIT 로봇에 resume 명령 발행
  _is_safe_to_resume(rid)    : claimed_nodes + current_node 충돌 체크
```

---

## 3. 맵 구조 (8×4 + 작업대 2개)

```
W1(33)── 1 ─ 2 ─ 3 ─ 4 ─ 5 ─ 6 ─ 7 ─ 8    (row 0, 통로)
         |   |   |   |   |   |   |   |
         9 ─10 ─[11]─[12]─13 ─[14]─[15]─16   (row 1, []=선반)
         |   |   |   |   |   |   |   |
        17 ─18 ─[19]─[20]─21 ─[22]─[23]─24   (row 2, []=선반)
         |   |   |   |   |   |   |   |
W2(34)──25 ─26 ─27 ─28 ─29 ─30 ─31 ─32    (row 3, 통로)
```

- **총 노드**: 34개 (32 그리드 + 2 작업대)
- **S (선반)**: 8개 — 11, 12, 14, 15, 19, 20, 22, 23
- **W (작업대)**: 2개 — 33(W1), 34(W2)
- **W1(33)**: gateway=1, staging=9, trigger=2
- **W2(34)**: gateway=25, staging=17, trigger=26

---

## 4. 통신 프로토콜

### MQTT 토픽 전체 목록

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 (노드 경로 + 타임스텝) |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/control` | Server → AGV | resume 명령 (NODE_WAIT 해제) |
| `/agv/arrived` | AGV → Server | 목표 노드 도착 / 중간 노드 위치 보고 |
| `/agv/shelf_ack` | AGV → Server | 리프트 동작 완료 알림 |
| `/agv/marker` | AGV → Server | ArUco 마커 인식 (트리거 노드 통과) |
| `agv/algorithm` | GUI/CLI → Server | UI 명령 수신 |

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

### 경로 발행 (MQTT `/agv/plan`)
```json
{
  "job_id": 1737886123,
  "planner": "astar_with_time",
  "robots": [
    {
      "rid": 1,
      "start": 33,
      "goal": 11,
      "node_path": [33, 1, 9, 10, 11],
      "timed_path": [{"node": 33, "t": 0}, ...]
    }
  ],
  "speed": 0.3
}
```

### 선반 리프트 명령 (MQTT `/agv/shelf_cmd`)
```json
{"rid": 1, "command": "pickup", "shelf_id": 11}
```

### resume 명령 (MQTT `/agv/control`)
```json
{"rid": 1, "cmd": "resume"}
```

### 로봇 도착/위치 보고 (MQTT `/agv/arrived`)
```json
// 최종 목표 도착
{"rid": 1, "node": 11}

// 중간 노드 위치 (수정 18)
{"rid": 1, "node": 10, "type": "robot_position"}
```

### 리프트 완료 (MQTT `/agv/shelf_ack`)
```json
{"rid": 1, "command": "pickup", "shelf_id": 11, "status": "done"}
```

### ArUco 마커 (MQTT `/agv/marker`)
```json
{"rid": 1, "marker_id": 2}
```

---

## 5. 로봇 상태 머신

```
IDLE → MOVING_TO_SHELF → PICKING_UP_SHELF → DELIVERING_TO_WS → WAITING_FOR_PICK
                                                                       │
                                          ┌────────────────────────────┤
                                          │                            │
                                   [다른 작업대도 필요]          [더이상 불필요]
                                          │                            │
                                   FORWARD_SHELF              RETURNING_SHELF
                                   (다른 작업대로)             (선반 홈으로)
                                          │                            │
                                   WAITING_FOR_PICK          [다음 선반 있음?]
                                          │                      Yes → MOVING_TO_SHELF
                                          │                      No  → IDLE
                                          └────────────────────────────┘
```

### shelf_ack에 의한 상태 전이

1. **GO_TO_SHELF 도착** → `shelf_cmd: pickup` 전송 → AGV가 리프트 올림
2. **`shelf_ack: pickup` 수신** → DELIVERING_TO_WS 상태, 작업대로 경로 계획 (STG 체크)
3. **RETURN/FORWARD 도착** → `shelf_cmd: putdown` 전송 → AGV가 리프트 내림
4. **`shelf_ack: putdown` 수신** → 다음 선반 or IDLE

### NODE_WAIT 흐름 (수정 19)

```
중간 노드 도착 → AGV 자동 정지 → robot_position 보고
서버 충돌 체크 → 안전하면 resume 발행 → AGV 재출발
```

---

## 6. 핵심 알고리즘 포인트

### F 노드 — 선반 가용성 6분기 (`_get_shelf_availability`)

| 상태 | 조건 | 반환 |
|------|------|------|
| IN_PLACE | 예약 없음 | `'go'` → 선반 홈으로 이동 |
| IN_PLACE | 다른 AGV GO_TO_SHELF 중 | `'pending'` → 순서 회전 시도 |
| CARRIED | 이동 중 | `'pending'` → 순서 회전 시도 |
| AT_WORKSTATION | carrier WAITING_FOR_PICK 중 | `'pending'` |
| AT_WORKSTATION | WS 회랑 점유 중 | `'pending'` |
| AT_WORKSTATION | 진입 가능 | `'direct'` → WS로 직행 |

### STG — 작업대 회랑 게이팅

- `_plan_and_publish_move()` 내에서 `should_stage()` 자동 호출
- ⚠️ staging 체크는 `start==goal` 즉시도착 처리보다 **반드시 먼저** 실행 (Bug B 교훈)

### TRG — 마커 트리거 + 위치 기반 해제 (수정 20)

- ArUco 마커 통과 시 `handle_marker_trigger()` → 대기 AGV 해제 (백업)
- 위치 기반: `is_exiting=True` 로봇이 회랑 구역({ws, gateway}) 밖으로 나가면 자동 해제 (주)

### Node U — 복귀 중 인터셉트

- `_try_intercept_returning_shelf()`: RETURNING_SHELF 로봇을 새 WS로 우회
- 인터셉트 시 기존 회랑 자동 해제

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

---

## 8. 설정 파일

### `config/robot_config.json`
```json
{
  "robots": {
    "1": {"home_node": 33, "name": "AGV-1"},
    "2": {"home_node": 34, "name": "AGV-2"}
  }
}
```

### `config/shelf_config.json`
```json
{
  "shelves": {
    "11": {"label": "1-1", "items": ["딸기", "드롭스", ...]},
    "12": {"label": "1-2", "items": ["사과", ...]},
    ...
  },
  "workstations": {
    "33": {"label": "W1", "gateway_node": 1, "staging_node": 9, "trigger_node": 2, "user_id": 1},
    "34": {"label": "W2", "gateway_node": 25, "staging_node": 17, "trigger_node": 26, "user_id": 2}
  }
}
```

### `config/map.json`
- 34개 노드 (8×4 그리드 + 작업대 2개)
- 노드 타입: M (통로), S (선반), W (작업대)
- 양방향 엣지, cost = 1
