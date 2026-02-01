# AGV Webots Simulation (v4)

AGV 기반 물류 피킹 시스템 - 선반 운반 + 다단계 작업 관리

## 디렉토리 구조

```
webots_simulation/
│
├── server/                         # 서버 (PC용)
│   ├── main.py                     # 서버 진입점
│   ├── config.py                   # 설정 관리
│   ├── path_planner.py             # A* 경로 계획
│   ├── task_manager.py             # 작업 분해/스케줄링
│   ├── robot_manager.py            # 로봇 상태 관리
│   ├── shelf_manager.py            # 선반 상태 관리
│   ├── request_handler.py          # 요청 처리
│   ├── websocket_handler.py        # WebSocket 서버
│   ├── mqtt_publisher.py           # MQTT 발행
│   ├── requirements.txt            # 서버 의존성
│   └── README.md
│
├── rpi/                            # RPi용 브릿지
│   ├── bridge.py                   # MQTT ↔ UART 브릿지
│   ├── requirements.txt            # RPi 의존성
│   └── README.md
│
├── config/                         # 공통 설정
│   ├── map.json                    # 7×7 그리드 맵
│   ├── robot_config.json           # 로봇 설정
│   ├── shelf_config.json           # 선반/물품 설정
│   └── README.md
│
├── controllers/                    # Webots 컨트롤러
│   ├── agv_controller/
│   └── agv_mqtt_controller/
│
├── worlds/                         # Webots 월드 파일
│
└── test_workflow.py                # 테스트 스크립트
```

---

## 빠른 시작

### 서버 (PC)

```bash
git clone https://github.com/.../TU_Capstone_Design.git
cd TU_Capstone_Design/webots_simulation

# 의존성 설치
pip install -r server/requirements.txt

# MQTT 브로커 설치
sudo apt install mosquitto
sudo systemctl start mosquitto

# 서버 실행
python -m server.main
```

### RPi

```bash
git clone https://github.com/.../TU_Capstone_Design.git
cd TU_Capstone_Design/webots_simulation/rpi

# 의존성 설치
pip install -r requirements.txt

# 설정 수정 (서버 IP 변경)
nano bridge.py   # MQTT_HOST = "서버IP"

# 실행
python bridge.py
```

---

## 시스템 구조

```
┌──────────────┐     WebSocket     ┌──────────────┐      MQTT       ┌──────────────┐
│  Admin UI    │ ────────────────> │   Server     │ ───────────────>│  RPi Bridge  │
│  (브라우저)   │     port 8765     │    (PC)      │   /agv/plan     │              │
└──────────────┘                   └──────────────┘                 └──────┬───────┘
                                          │                                │ UART
                                          │                                ▼
                                          │                         ┌──────────────┐
                                          │                         │    STM32     │
                                          │                         │  (모터 제어)  │
                                          │                         └──────────────┘
                                          │
                                          ├─ path_planner: A* 경로 계획
                                          ├─ task_manager: 작업 분해
                                          ├─ robot_manager: 로봇 상태
                                          └─ shelf_manager: 선반 관리
```

---

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
- **W (작업대)**: 2개 - 50(W1), 51(W2)

---

## API 사용법

### 주문 시작
```json
{"type": "start_order", "사용자ID": 1, "주문번호": 1}
```

### 물품 픽업 (선반-서랍)
```json
{"type": "shelf_complete", "사용자ID": 1, "선반번호": "1-1"}
```

### 주문 완료 확인
```json
{"type": "order_complete", "사용자ID": 1, "주문번호": 1}
```

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

## 시뮬레이션 테스트

```bash
# 터미널 1: 서버
python -m server.main

# 터미널 2: Bridge (시뮬레이션 모드)
cd rpi && python bridge.py

# 터미널 3: Webots
webots worlds/agv_warehouse.wbt

# 터미널 4: 테스트
python test_workflow.py
```

---

## 폴더별 상세 문서

| 폴더 | README |
|------|--------|
| `server/` | 서버 설치/실행/API 상세 |
| `rpi/` | RPi 설정/UART 프로토콜 |
| `config/` | 맵/선반/로봇 설정 |

---

## 필수 패키지

| 구분 | 패키지 |
|------|--------|
| **서버** | `paho-mqtt`, `websockets`, `mosquitto` |
| **RPi** | `paho-mqtt`, `pyserial` |
