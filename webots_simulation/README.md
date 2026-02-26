# AGV Webots Simulation (v4)

AGV 기반 물류 피킹 시스템 - KIVA 선반 운반 + 다단계 작업 관리

## 빠른 시작

### 1. 의존성 설치
```bash
git clone git@github.com:2020134026LWJ/TU_Capstone_Design.git
cd TU_Capstone_Design/webots_simulation
pip install -r server/requirements.txt
```

### 2. MQTT 브로커
```bash
sudo apt install mosquitto
sudo systemctl start mosquitto
```

### 3. 실행 (터미널 3개)

```bash
# 터미널 1: 서버
python3 -m server.main

# 터미널 2: Webots 시뮬레이션
webots worlds/warehouse_7x7.wbt

# 터미널 3: CLI 테스트
python3 websocket_test.py
```

---

## CLI 명령어 (websocket_test.py)

| 명령어 | 설명 |
|--------|------|
| `시작 1` | 사용자1 주문 시작 (AGV-1, W1) |
| `시작 2` | 사용자2 주문 시작 (AGV-2, W2) |
| `완료 1` | 사용자1 선반 수령 완료 → 다음 선반 |
| `완료 2` | 사용자2 선반 수령 완료 → 다음 선반 |
| `테스트` | 포워딩 테스트 (겹치는 선반 주문) |
| `상태` | 로봇/주문 상태 조회 |
| `종료` | 프로그램 종료 |

---

## 디렉토리 구조

```
webots_simulation/
│
├── server/                         # 서버 (Python)
│   ├── main.py                     # 서버 진입점
│   ├── config.py                   # 설정 관리
│   ├── request_handler.py          # 요청 처리 (주문, 도착, shelf_ack)
│   ├── path_planner.py             # Prioritized A* 경로 계획
│   ├── task_manager.py             # 작업 분해/스케줄링
│   ├── task_scheduler.py           # Nearest Neighbor 최적화
│   ├── robot_manager.py            # 로봇 상태 관리 (6단계)
│   ├── shelf_manager.py            # 선반 상태 관리
│   ├── db_loader.py                # 엑셀 DB 로더
│   ├── websocket_handler.py        # WebSocket 서버
│   ├── mqtt_publisher.py           # MQTT 발행
│   └── requirements.txt            # 의존성 (paho-mqtt, websockets)
│
├── controllers/                    # Webots 컨트롤러
│   ├── agv_controller/             # 기본 테스트용
│   └── agv_mqtt_controller/        # MQTT + Supervisor + 리프트
│       ├── agv_mqtt_controller.py
│       └── paho/                   # paho-mqtt (Webots 내장 Python용)
│
├── config/                         # 설정 파일
│   ├── map.json                    # 7x7 그리드 맵 (51노드)
│   ├── robot_config.json           # 로봇 설정 (AGV-1, AGV-2)
│   └── shelf_config.json           # 선반/물품/작업대 설정
│
├── worlds/                         # Webots 월드 파일
│   └── warehouse_7x7.wbt          # 현재 월드 (KIVA 선반 + AGV 리프트)
│
├── Database/                       # 주문 엑셀 데이터
│   ├── 데이터 베이스.xlsx
│   ├── 사용자1주문.xlsx
│   └── 사용자2주문.xlsx
│
├── rpi/                            # RPi 브릿지 (실제 하드웨어용)
│
├── websocket_test.py               # CLI 테스트 도구 (주문/완료/상태)
├── test_forward.py                 # 포워딩 테스트 스크립트
└── test_dual_order.py              # 듀얼 주문 테스트 스크립트
```

---

## 시스템 구조

```
                                ┌──────────────────────────────┐
                                │          Server (PC)         │
┌──────────────┐   WebSocket    │                              │    MQTT
│ CLI 테스트    │ ─────────────>│  request_handler             │──────────────>  AGV (Webots)
│ websocket_   │   port 8765   │  task_manager                │  /agv/plan       ├─ AGV-1
│ test.py      │               │  task_scheduler              │  /agv/shelf_cmd  └─ AGV-2
└──────────────┘               │  path_planner                │
                               │  robot_manager               │<──────────────
                               │  shelf_manager               │  /agv/arrived
                               │  db_loader                   │  /agv/shelf_ack
                               └──────────────────────────────┘
```

### MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 (노드 경로 + 타임스텝) |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/arrived` | AGV → Server | 목표 노드 도착 알림 |
| `/agv/shelf_ack` | AGV → Server | 리프트 동작 완료 알림 |

---

## 맵 구조 (7x7 + 작업대 2개)

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
- **W (작업대)**: 2개 - 50(W1), 51(W2)

---

## 로봇 상태 머신

```
IDLE → MOVING_TO_SHELF → PICKING_UP_SHELF → DELIVERING_TO_WS → WAITING_FOR_PICK
                                                                       │
                                          ┌────────────────────────────┤
                                          │                            │
                                   [다른 WS 필요]                 [불필요]
                                          │                            │
                                   FORWARD_SHELF              RETURNING_SHELF
                                          │                            │
                                   WAITING_FOR_PICK          [다음 선반?]
                                                          Yes → MOVING_TO_SHELF
                                                          No  → IDLE
```

---

## 폴더별 상세 문서

| 폴더 | README |
|------|--------|
| `server/` | 서버 모듈 설명, API, 통신 프로토콜 |
| `controllers/` | AGV 컨트롤러, Supervisor, 리프트 |
| `worlds/` | 월드 파일, KIVA 선반, AGV 구조 |
| `config/` | 맵/선반/로봇 설정 |

---

## 필수 패키지

| 패키지 | 용도 |
|--------|------|
| `paho-mqtt` | MQTT 클라이언트 (서버↔AGV 통신) |
| `websockets` | WebSocket 서버 (서버↔CLI 통신) |
| `mosquitto` | MQTT 브로커 (시스템 패키지) |
| `pandas` | 엑셀 파일 읽기 (db_loader) |
| `openpyxl` | pandas의 xlsx 엔진 |
