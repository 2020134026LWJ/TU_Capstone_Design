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
webots worlds/warehouse_7x7.wbt

# 터미널 3: CLI 테스트
python3 websocket_test.py
```

---

## 1. 전체 시스템 구조

```
┌─────────────┐     WebSocket      ┌─────────────┐      MQTT       ┌─────────────┐
│  CLI 테스트  │ ──────────────────>│   Server    │ ───────────────>│ AGV (Webots)│
│  (관리자)    │    port 8765       │  (이 서버)   │   /agv/plan     │  Supervisor │
└─────────────┘                    └─────────────┘   /agv/shelf_cmd └──────┬──────┘
                                          │                                │
                                          │                                │ /agv/arrived
                                          │                                │ /agv/shelf_ack
                                          │<───────────────────────────────┘
                                          │
                                          ├─ shelf_manager   : 선반 위치/물품 추적
                                          ├─ task_manager    : 작업 분해/스케줄링
                                          ├─ task_scheduler  : Nearest Neighbor 최적화
                                          ├─ path_planner    : Prioritized A* (충돌 회피)
                                          ├─ robot_manager   : 6단계 상태 머신
                                          └─ db_loader       : 엑셀 DB 로더
```

**데이터 흐름:**
1. CLI에서 주문 시작 (start_order) → DB에서 물품 목록 로드
2. Server가 물품→선반 매핑 후 Nearest Neighbor로 방문 순서 최적화
3. 유휴 로봇에 작업 배정 → Prioritized A* 경로 계획
4. MQTT `/agv/plan`으로 경로 전송 → AGV가 직접 수신·주행
5. AGV 도착 (`/agv/arrived`) → 선반 리프트 명령 (`/agv/shelf_cmd`)
6. AGV 리프트 완료 (`/agv/shelf_ack`) → 작업대로 배달
7. 작업자 픽업 완료 → 선반 복귀 또는 다른 작업대로 포워딩


## 2. 모듈별 역할

### 파일 구조
```
server/
├── __init__.py          # 패키지 초기화
├── config.py            # 설정값 관리
├── main.py              # 서버 시작점
├── websocket_handler.py # WebSocket 통신 (CLI / Admin UI)
├── request_handler.py   # 요청 처리 (주문, 픽완료, 도착, shelf_ack)
├── path_planner.py      # 경로 계획 (Prioritized A*, 선반 통과 제외)
├── mqtt_publisher.py    # MQTT 발행
├── robot_manager.py     # 로봇 상태 관리 (6단계 상태머신)
├── shelf_manager.py     # 선반 상태 관리 (위치, 물품, 운반)
├── task_manager.py      # 작업 분해 및 스케줄링
├── task_scheduler.py    # Nearest Neighbor 선반 방문 순서 최적화
└── db_loader.py         # 엑셀 DB 로더 (주문 데이터)
```

### 각 모듈 설명

#### `config.py` - 설정 관리
```
- MQTT 호스트/포트: localhost:1883
- WebSocket 포트: 8765
- 맵 파일: config/map.json (7x7 + 작업대 2개)
- 선반 설정: config/shelf_config.json
- 로봇 설정: config/robot_config.json
- MQTT 토픽: /agv/plan, /agv/shelf_cmd, /agv/arrived, /agv/shelf_ack
```

#### `shelf_manager.py` - 선반 관리
```
- 선반별 물품 목록 관리
- 물품 → 선반 매핑 (find_shelves_for_items)
- 선반 상태 추적: IN_PLACE, CARRIED, AT_WORKSTATION
- 빈 선반 위치 탐색 (가장 가까운 빈 자리)
```

#### `task_manager.py` - 작업 관리
```
- 배치 작업 등록 (여러 물품)
- 작업 분해: 물품 → 선반 → 서브태스크 순서
- 픽업 완료 처리 (item by item)
- 선반 포워딩 감지 (다른 작업대도 필요시)

서브태스크 타입:
- GO_TO_SHELF: 선반으로 이동
- PICKUP_SHELF: 리프트 상승
- DELIVER_TO_WS: 작업대로 배달
- WAIT_PICKING: 픽업 대기
- RETURN_SHELF: 선반 복귀
- FORWARD_SHELF: 다른 작업대로 포워딩
```

#### `task_scheduler.py` - 작업 최적화
```
- Nearest Neighbor 알고리즘으로 선반 방문 순서 최적화
- 로봇 현재 위치에서 가장 가까운 선반부터 방문
```

#### `db_loader.py` - 엑셀 DB 로더
```
- Database/ 디렉토리의 엑셀 파일 로드 (pandas)
- 사용자별 주문 데이터 → 물품 목록 변환
- 재고 정보 로드/수정
- 물품명 → 선반 노드 매핑
```

#### `robot_manager.py` - 로봇 관리
```
상태 머신 (6단계):
- IDLE: 대기
- MOVING_TO_SHELF: 선반으로 이동 중
- PICKING_UP_SHELF: 리프트 상승 중
- DELIVERING_TO_WS: 작업대로 배달 중
- WAITING_FOR_PICK: 픽업 대기
- RETURNING_SHELF: 선반 복귀 중
```

#### `path_planner.py` - 경로 계획
```
- map.json 로드 (노드 타입 포함)
- Prioritized A* (시간 기반 충돌 회피)
- 선반 노드 통과 제외 (출발/도착만 허용)
- 다중 로봇 동시 경로 계획
```

#### `request_handler.py` - 요청 처리
```
지원하는 요청 타입:

[주문 API]
1. start_order         - 주문 시작 (DB/엑셀 연동)
2. shelf_complete      - 선반/서랍 물품 픽업 완료
3. order_complete      - 주문 완료 확인

[내부/레거시 API]
4. batch_task_request  - 배치 작업 등록
5. pick_complete       - 물품 픽업 완료
6. status_request      - 전체 상태 조회
7. task_status_request - 작업 상세 조회
8. shelf_status_request - 선반 상세 조회

[MQTT 수신]
9. robot_arrived       - 로봇 도착 알림 (/agv/arrived)
10. shelf_ack          - 리프트 완료 알림 (/agv/shelf_ack)
```

---

## 3. 맵 구조 (7x7 + 작업대 2개)

```
W1(50)─ 1   2   3   4   5   6   7     (row 0, 통로)
        8  [9] 10 [11] 12 [13] 14     (row 1, []=선반)
       15  16  17  18  19  20  21     (row 2, 통로)
       22 [23] 24 [25] 26 [27] 28     (row 3, []=선반)
       29  30  31  32  33  34  35     (row 4, 통로)
       36 [37] 38 [39] 40 [41] 42     (row 5, []=선반)
W2(51)─43  44  45  46  47  48  49     (row 6, 통로)
```

- **총 노드**: 51개 (49 그리드 + 2 작업대)
- **M (통로)**: 40개 - 로봇 이동 경로
- **S (선반)**: 9개 - 9, 11, 13, 23, 25, 27, 37, 39, 41
- **W (작업대)**: 2개 - 50(W1, 상단), 51(W2, 하단)


## 4. 통신 프로토콜

### MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 (노드 경로 + 타임스텝) |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/arrived` | AGV → Server | 목표 노드 도착 알림 |
| `/agv/shelf_ack` | AGV → Server | 리프트 동작 완료 알림 |

### 주문 API (WebSocket)

**주문 시작:**
```json
// 요청
{"type": "start_order", "사용자ID": 1, "주문번호": 1}

// 응답
{
  "type": "start_order_response",
  "success": true,
  "사용자ID": 1,
  "주문번호": 1,
  "task_id": "ORDER_1_1",
  "items": ["A", "B", "C"],
  "message": "주문 1 작업 시작"
}
```

**선반/서랍 물품 픽업:**
```json
// 요청 (선반번호 형식: "선반ID-서랍번호")
{"type": "shelf_complete", "사용자ID": 1, "선반번호": "1-1"}

// 응답
{
  "type": "shelf_complete_response",
  "success": true,
  "사용자ID": 1,
  "선반번호": "1-1",
  "item": "A",
  "action": "continue_picking",
  "remaining_items": ["B", "C"]
}
```

### 경로 발행 (MQTT `/agv/plan`)
```json
{
  "job_id": 1737886123,
  "planner": "prioritized_astar_with_time_on_graph",
  "robots": [
    {
      "rid": 1,
      "start": 50,
      "goal": 9,
      "node_path": [50, 1, 2, 3, 10, 9],
      "timed_path": [{"node": 50, "t": 0}, ...]
    }
  ],
  "speed": 0.3
}
```

### 선반 리프트 명령 (MQTT `/agv/shelf_cmd`)
```json
{"rid": 1, "command": "pickup", "shelf_id": 9}
```

### 리프트 완료 알림 (MQTT `/agv/shelf_ack`)
```json
{"rid": 1, "command": "pickup", "shelf_id": 9, "status": "done"}
```

### 로봇 도착 알림 (MQTT `/agv/arrived`)
```json
{"rid": 1, "node": 9}
```


## 5. 로봇 상태 머신

```
IDLE → MOVING_TO_SHELF → PICKING_UP_SHELF → DELIVERING_TO_WS → WAITING_FOR_PICK
                                                                       │
                                          ┌────────────────────────────┤
                                          │                            │
                                   [다른 작업대도 필요]          [더이상 불필요]
                                          │                            │
                                   FORWARD_SHELF              RETURNING_SHELF
                                   (다른 작업대로)             (가장 가까운 빈자리로)
                                          │                            │
                                   WAITING_FOR_PICK          [다음 선반 있음?]
                                          │                      Yes → MOVING_TO_SHELF
                                          │                      No  → IDLE (작업 완료)
                                          └────────────────────────────┘
```

### shelf_ack에 의한 상태 전이

선반 리프트 동작은 비동기적으로 처리된다:

1. **GO_TO_SHELF 도착** → `shelf_cmd: pickup` 전송 → AGV가 리프트 올림
2. **`shelf_ack: pickup` 수신** → DELIVER_TO_WS 상태로 전이, 작업대로 경로 계획
3. **RETURN/FORWARD 도착** → `shelf_cmd: putdown` 전송 → AGV가 리프트 내림
4. **`shelf_ack: putdown` 수신** → 선반 내려놓기 완료, 다음 작업 또는 IDLE


## 6. 의존성

```
websockets>=10.0    # WebSocket 서버
paho-mqtt>=1.6.0    # MQTT 클라이언트
pandas              # 엑셀 파일 읽기 (db_loader)
openpyxl            # pandas의 xlsx 엔진
```

설치:
```bash
pip install -r requirements.txt
```


## 7. 설정 파일

### `config/robot_config.json`
```json
{
  "robots": {
    "1": {"home_node": 50, "name": "AGV-1"},
    "2": {"home_node": 51, "name": "AGV-2"}
  }
}
```

### `config/shelf_config.json`
```json
{
  "shelves": {
    "9":  {"label": "S1", "items": ["A", "B", "C"]},
    "11": {"label": "S2", "items": ["D", "E", "F"]},
    ...
  },
  "workstations": {
    "50": {"label": "W1", "gateway_node": 1},
    "51": {"label": "W2", "gateway_node": 43}
  }
}
```

### `config/map.json`
- 51개 노드 (7x7 그리드 + 작업대 2개)
- 노드 타입: M (통로), S (선반), W (작업대)
- 양방향 엣지, cost = 1
