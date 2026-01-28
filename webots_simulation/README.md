# AGV Webots Simulation (v4)

AGV 기반 물류 피킹 시스템 - 선반 운반 + 다단계 작업 관리

## 디렉토리 구조

```
webots_simulation/
│
├── server/                         # 모듈화된 AGV 서버
│   ├── main.py                     # 서버 진입점
│   ├── config.py                   # 설정 관리
│   ├── path_planner.py             # A* 경로 계획 (선반 통과 제외)
│   ├── mqtt_publisher.py           # MQTT 발행
│   ├── websocket_handler.py        # WebSocket 서버 (Admin UI)
│   ├── request_handler.py          # 요청 처리 (배치작업, 픽완료, 도착)
│   ├── robot_manager.py            # 로봇 상태 관리 (6단계 상태머신)
│   ├── shelf_manager.py            # 선반 상태 관리 (위치, 물품, 운반)
│   ├── task_manager.py             # 작업 분해 및 스케줄링
│   └── __init__.py
│
├── controllers/                    # Webots 컨트롤러
│   ├── agv_controller/
│   └── agv_mqtt_controller/
│
├── worlds/                         # Webots 월드 파일
│
├── bridge.py                       # 양방향 브릿지 (MQTT ↔ UART)
├── map.json                        # 7×7 그리드 맵 (51노드)
├── shelf_config.json               # 선반별 물품 매핑
└── robot_config.json               # 로봇 설정
```

## 시스템 흐름

```
[Admin UI] ──WebSocket──> [Server] ──MQTT──> [Bridge(RPi)] ──UART──> [STM32]
  (8765)                     │                    │
                             │                    ├─ /agv/plan → MOVE_TO_NODE
                             │                    ├─ /agv/shelf_cmd → LIFT_UP/DOWN
                             │                    │
                             │                    ├─ MOVE_DONE → /agv/arrived
                             │                    └─ LIFT_DONE → /agv/state
                             │
                             ├─ shelf_manager: 선반 위치/상태 추적
                             ├─ task_manager: 작업 분해/서브태스크 관리
                             └─ path_planner: A* (선반 노드 통과 제외)
```

## 맵 구조 (7×7 + 작업대 2개)

```
W1(50)─ 1   2   3   4   5   6   7     (row 0, 통로)
        8  [9] 10 [11] 12 [13] 14     (row 1, []=선반)
       15  16  17  18  19  20  21     (row 2, 통로)
       22 [23] 24 [25] 26 [27] 28     (row 3, []=선반)
       29  30  31  32  33  34  35     (row 4, 통로)
       36 [37] 38 [39] 40 [41] 42     (row 5, []=선반)
W2(51)─43  44  45  46  47  48  49     (row 6, 통로)
```

- **M (통로)**: 40개 - 로봇 이동 경로
- **S (선반)**: 9개 - 9, 11, 13, 23, 25, 27, 37, 39, 41
- **W (작업대)**: 2개 - 50(W1, 상단), 51(W2, 하단)
- 로봇은 통로(M)로만 이동, 선반(S)은 출발/도착지로만 접근

## 로봇 상태 머신

```
IDLE → MOVING_TO_SHELF → PICKING_UP_SHELF → DELIVERING_TO_WS → WAITING_FOR_PICK
                                                                       │
                                          ┌────────────────────────────┤
                                          │                            │
                                   [다른 작업대 필요]            [불필요]
                                          │                            │
                                   FORWARD (다른WS)        RETURNING_SHELF
                                          │               (가장 가까운 빈자리)
                                          │                            │
                                   WAITING_FOR_PICK          [다음 선반?]
                                                          Yes → MOVING_TO_SHELF
                                                          No  → IDLE
```

## 작업 흐름

1. Admin UI에서 배치 작업 등록 (물품 목록)
2. 서버가 물품→선반 매핑 후 서브태스크 분해
3. 유휴 로봇 배정 → 선반으로 이동 → 리프트 → 작업대 배달
4. 작업자가 물품별 픽업 완료 신호 전송
5. 해당 선반의 모든 물품 픽업 후:
   - 다른 작업대에서도 필요 → 포워딩
   - 아니면 → 가장 가까운 빈 선반 자리에 복귀
6. 다음 선반 반복 → 작업 완료

## 실행 방법

```bash
# 1. 서버 실행
cd webots_simulation
python -m server.main

# 2. 브릿지 실행
python bridge.py

# 3. Webots 시뮬레이션
webots worlds/warehouse_9x5.wbt
```

## WebSocket 요청 형식

### 배치 작업 등록
```json
{
  "type": "batch_task_request",
  "tasks": [
    {"task_id": "T1", "workstation_id": 50, "items": ["A", "B", "Z", "D"]},
    {"task_id": "T2", "workstation_id": 51, "items": ["C", "X", "U", "I"]}
  ]
}
```

### 물품 픽업 완료
```json
{"type": "pick_complete", "task_id": "T1", "item": "A", "workstation_id": 50}
```

### 상태 조회
```json
{"type": "status_request"}
```

## UART 통신 프로토콜 (RPi ↔ STM32)

```
패킷: [0xAA] [CMD] [LEN] [PAYLOAD...] [CRC]
CRC = CMD ^ LEN ^ payload[0] ^ ...
```

| 방향 | CMD | 이름 | 설명 |
|------|-----|------|------|
| RPi→STM | 0x01 | MOVE_TO_NODE | 노드 이동 명령 |
| RPi→STM | 0x02 | STOP | 즉시 정지 |
| RPi→STM | 0x03 | LIFT_UP | 선반 리프트 상승 |
| RPi→STM | 0x04 | LIFT_DOWN | 선반 리프트 하강 |
| STM→RPi | 0x81 | MOVE_DONE | 이동 완료 |
| STM→RPi | 0x83 | LIFT_DONE | 리프트 완료 |
| STM→RPi | 0x85 | MARKER_PASSED | 마커 통과 |
| STM→RPi | 0xFF | ACK | 명령 수신 확인 |

## 설정 파일

| 파일 | 설명 |
|------|------|
| `map.json` | 7×7 그리드 + 작업대, 노드 타입(M/S/W), 엣지 |
| `shelf_config.json` | 선반 9개 × 물품 3개 매핑, 작업대 설정 |
| `robot_config.json` | 로봇 ID, 이름, 홈 노드 (설정 가능) |
| `server/config.py` | MQTT/WebSocket 포트, 경로 계획 파라미터 |

## 필수 설치

```bash
sudo apt install mosquitto           # MQTT 브로커
pip install websockets paho-mqtt     # Python 패키지
pip install pyserial                 # UART (실제 하드웨어)
```
