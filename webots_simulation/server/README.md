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

### 3. 서버 실행
```bash
cd webots_simulation
python -m server.main
```

### 4. 시뮬레이션 테스트
```bash
# 터미널 1: 서버
python -m server.main

# 터미널 2: Bridge (시뮬레이션 모드)
cd rpi && python bridge.py

# 터미널 3: Webots
webots worlds/agv_warehouse.wbt

# 터미널 4: 테스트 요청
python test_workflow.py
```

---

## 1. 전체 시스템 구조

```
┌─────────────┐     WebSocket      ┌─────────────┐      MQTT       ┌─────────────┐
│  Admin UI   │ ──────────────────>│   Server    │ ───────────────>│  bridge.py  │
│  (관리자)    │    port 8765       │  (이 서버)   │   /agv/plan     │   (RPi)     │
└─────────────┘                    └─────────────┘   /agv/shelf_cmd └──────┬──────┘
                                          │                                │
                                          │                                │ UART
                                          │                                │ (바이너리 패킷)
                                          │                                v
                                          │                         ┌─────────────┐
                                          │                         │    STM32    │
                                          │                         │ (모터 제어)  │
                                          │                         └─────────────┘
                                          │
                                          ├─ shelf_manager: 선반 위치/물품 추적
                                          ├─ task_manager: 작업 분해/스케줄링
                                          ├─ path_planner: A* (선반 노드 통과 제외)
                                          └─ robot_manager: 6단계 상태 머신
```

**데이터 흐름:**
1. Admin UI에서 배치 작업 등록 (물품 목록)
2. Server가 물품→선반 매핑 후 서브태스크 분해
3. 유휴 로봇에 작업 배정 → 경로 계획 (A*)
4. MQTT로 이동 명령 전송 → bridge.py → STM32
5. 로봇 도착 → 리프트 → 작업대 배달 → 픽업 대기
6. 작업자 픽업 완료 신호 → 선반 복귀 또는 포워딩


## 2. 모듈별 역할

### 📁 파일 구조
```
server/
├── __init__.py          # 패키지 초기화
├── config.py            # 설정값 관리
├── main.py              # 서버 시작점
├── websocket_handler.py # WebSocket 통신 (Admin UI)
├── request_handler.py   # 요청 처리 (배치작업, 픽완료, 도착)
├── path_planner.py      # 경로 계획 (A*, 선반 통과 제외)
├── mqtt_publisher.py    # MQTT 발행
├── robot_manager.py     # 로봇 상태 관리 (6단계 상태머신)
├── shelf_manager.py     # 선반 상태 관리 (위치, 물품, 운반)
└── task_manager.py      # 작업 분해 및 스케줄링
```

### 각 모듈 설명

#### `config.py` - 설정 관리
```python
- MQTT 호스트/포트: localhost:1883
- WebSocket 포트: 8765
- 맵 파일: map.json (7×7 + 작업대 2개)
- 선반 설정: shelf_config.json
- 로봇 설정: robot_config.json
- MQTT 토픽: /agv/plan, /agv/shelf_cmd, /agv/state, /agv/arrived
```

#### `shelf_manager.py` - 선반 관리 (v4 신규)
```
역할:
- 선반별 물품 목록 관리
- 물품 → 선반 매핑 (find_shelves_for_items)
- 선반 상태 추적: IN_PLACE, CARRIED, AT_WORKSTATION
- 빈 선반 위치 탐색 (가장 가까운 빈 자리)
```

#### `task_manager.py` - 작업 관리 (v4 신규)
```
역할:
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

#### `robot_manager.py` - 로봇 관리
```
상태 머신 (6단계):
- IDLE: 대기
- MOVING_TO_SHELF: 선반으로 이동 중
- PICKING_UP_SHELF: 리프트 상승 중
- DELIVERING_TO_WS: 작업대로 배달 중
- WAITING_FOR_PICK: 픽업 대기
- RETURNING_SHELF: 선반 복귀 중

관리 정보:
- rid, name, home_node
- current_node, status
- current_task_id, carrying_shelf
```

#### `path_planner.py` - 경로 계획
```
기능:
- map.json 로드 (노드 타입 포함)
- A* 알고리즘 (시간 기반 충돌 회피)
- 선반 노드 통과 제외 (출발/도착만 허용)
- 다중 로봇 Prioritized Planning

노드 타입:
- M (Marker): 통로 - 이동 가능
- S (Shelf): 선반 - 출발/도착만 가능, 통과 불가
- W (Workstation): 작업대
```

#### `request_handler.py` - 요청 처리
```
지원하는 요청 타입:

[신규 주문 API]
1. start_order         - 주문 시작 (DB/엑셀 연동)
2. shelf_complete      - 선반/서랍 물품 픽업 완료
3. order_complete      - 주문 완료 확인

[내부/레거시 API]
4. batch_task_request  - 배치 작업 등록
5. pick_complete       - 물품 픽업 완료
6. robot_arrived       - 로봇 도착 알림 (MQTT에서)
7. status_request      - 전체 상태 조회
8. task_status_request - 작업 상세 조회
9. shelf_status_request - 선반 상세 조회
```


## 3. 맵 구조 (7×7 + 작업대 2개)

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

### 신규 주문 API (권장)

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

**주문 완료 확인:**
```json
// 요청
{"type": "order_complete", "사용자ID": 1, "주문번호": 1}

// 응답
{
  "type": "order_complete_response",
  "success": true,
  "사용자ID": 1,
  "주문번호": 1,
  "is_complete": true,
  "status": "completed",
  "message": "주문 완료"
}
```

---

### Admin UI → Server (WebSocket) - 레거시 API

**배치 작업 등록:**
```json
{
  "type": "batch_task_request",
  "tasks": [
    {"task_id": "T1", "workstation_id": 50, "items": ["A", "B", "Z", "D"]},
    {"task_id": "T2", "workstation_id": 51, "items": ["C", "X", "U", "I"]}
  ]
}
```

**물품 픽업 완료:**
```json
{
  "type": "pick_complete",
  "task_id": "T1",
  "item": "A",
  "workstation_id": 50
}
```

**상태 조회:**
```json
{"type": "status_request"}
{"type": "task_status_request"}
{"type": "shelf_status_request"}
```

### Server → Admin UI (WebSocket)

**배치 작업 응답:**
```json
{
  "type": "batch_task_response",
  "success": true,
  "tasks_created": 2,
  "tasks": [
    {
      "task_id": "T1",
      "workstation_id": 50,
      "items": ["A", "B", "Z", "D"],
      "shelves_needed": [9, 11, 41],
      "status": "in_progress",
      "assigned_robot": 1
    }
  ],
  "assignments": [
    {"robot_id": 1, "task_id": "T1", "first_target": 9}
  ]
}
```

**픽업 완료 응답:**
```json
{
  "type": "pick_complete_response",
  "success": true,
  "task_id": "T1",
  "item": "A",
  "action": "continue_picking",
  "remaining_items_on_shelf": ["B"],
  "total_remaining": 3
}
```

**선반 작업 지시:**
```json
{
  "type": "pick_complete_response",
  "action": "forward_shelf",
  "forward_to_ws": 51,
  "next_action": "forward_shelf"
}
```
또는
```json
{
  "action": "return_shelf",
  "return_to": 9,
  "next_action": "return_shelf"
}
```

### Server → bridge.py (MQTT)

**경로 발행:** `/agv/plan`
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

**선반 명령:** `/agv/shelf_cmd`
```json
{
  "rid": 1,
  "command": "lift_up",
  "shelf_id": 9
}
```

### bridge.py → Server (MQTT)

**로봇 도착:** `/agv/arrived`
```json
{"rid": 1, "node": 9}
```

**로봇 상태:** `/agv/state`
```json
{"rid": 1, "node": 9, "status": "idle"}
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


## 6. 실행 방법

### 서버 실행
```bash
cd /home/lwj/Projects/TU_Capstone_Design/webots_simulation
python -m server.main
```

### 테스트 (MQTT 없이)
```bash
python test_workflow.py
```

### 전체 시스템 테스트
```bash
# 터미널 1: 서버 실행
python -m server.main

# 터미널 2: bridge.py 실행
python bridge.py

# 터미널 3: Webots 시뮬레이션
webots worlds/warehouse_9x5.wbt
```


## 7. 의존성

```
websockets>=10.0    # WebSocket 서버
paho-mqtt>=2.0      # MQTT 클라이언트
```

설치:
```bash
pip install websockets paho-mqtt
```


## 8. 설정 파일

### `robot_config.json`
```json
{
  "robots": {
    "1": {"home_node": 50, "name": "AGV-1"},
    "2": {"home_node": 51, "name": "AGV-2"}
  }
}
```

### `shelf_config.json`
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

### `map.json`
- 51개 노드 (7×7 그리드 + 작업대 2개)
- 노드 타입: M (통로), S (선반), W (작업대)
- 양방향 엣지, cost = 1
