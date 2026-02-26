# AGV Webots Simulation (v4)

AGV 기반 물류 피킹 시스템 — KIVA 선반 운반 + 다단계 작업 관리

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
webots worlds/warehouse_4x8.wbt

# 터미널 3: CLI 테스트
python3 mqtt_test.py
```

---

## CLI 명령어 (mqtt_test.py)

| 명령어 | 설명 |
|--------|------|
| `시작 1` | 사용자1 주문 시작 (AGV-1, W1=노드33) |
| `시작 2` | 사용자2 주문 시작 (AGV-2, W2=노드34) |
| `완료 1` | 사용자1 선반 피킹 완료 → 선반 반납/포워딩 |
| `완료 2` | 사용자2 선반 피킹 완료 → 선반 반납/포워딩 |
| `선반완료 1 [노드번호]` | 특정 선반 피킹 완료 (shelf_complete 직접 지정) |
| `종료` | 프로그램 종료 |

> 선반 노드 번호: `11(1-1) 12(1-2) 14(1-3) 15(1-4) 19(2-1) 20(2-2) 22(2-3) 23(2-4)`

---

## 디렉토리 구조

```
webots_simulation/
│
├── server/                         # 서버 (Python)
│   ├── main.py                     # 서버 진입점 (WebSocket + MQTT 서버)
│   ├── config.py                   # 설정 관리
│   ├── request_handler.py          # ★ 핵심: 요청 처리 + 로봇 배정 알고리즘
│   ├── task_manager.py             # 작업 분해/스케줄링 (서브태스크 시퀀스)
│   ├── task_scheduler.py           # Nearest Neighbor 선반 방문 순서 최적화
│   ├── robot_manager.py            # 로봇 6단계 상태 머신
│   ├── shelf_manager.py            # 선반 상태 관리 (IN_PLACE/CARRIED/AT_WORKSTATION)
│   ├── staging_manager.py          # 작업대 회랑 게이팅 (STG/TRG/위치 기반 해제)
│   ├── path_planner.py             # A* 시간 기반 경로 계획 (예약 기반 충돌 회피)
│   ├── mqtt_publisher.py           # MQTT 발행 (/agv/plan, /agv/shelf_cmd, /agv/control)
│   ├── websocket_handler.py        # WebSocket 서버 (Admin UI용)
│   ├── db_loader.py                # 엑셀 DB 로더
│   └── requirements.txt            # 의존성
│
├── controllers/                    # Webots 컨트롤러
│   ├── agv_controller/             # 기본 테스트용 (MQTT 없이 단독 실행)
│   └── agv_mqtt_controller/        # MQTT + Supervisor + 리프트 (현재 사용)
│       ├── main.py                 # 진입점
│       ├── agv_controller.py       # 메인 AGV 로직 (마커감지, 리프트, NODE_WAIT)
│       ├── aruco_detector.py       # ArUco 마커 감지
│       ├── mqtt_handler.py         # MQTT 발행/수신
│       ├── navigation.py           # 경로추종 + NODE_WAIT 상태 관리
│       └── hardware/               # 실물/시뮬 하드웨어 추상화 레이어
│
├── config/                         # 설정 파일
│   ├── map.json                    # 8×4 그리드 맵 (34노드)
│   ├── robot_config.json           # 로봇 설정 (AGV-1 home=33, AGV-2 home=34)
│   └── shelf_config.json           # 선반/물품/작업대 설정
│
├── worlds/                         # Webots 월드 파일
│   └── warehouse_4x8.wbt           # 현재 월드 (KIVA 선반 8개 + AGV 2대 + ArUco 마커)
│
├── Database/                       # 주문 엑셀 데이터
│   ├── 데이터 베이스.xlsx
│   ├── 사용자1주문.xlsx
│   └── 사용자2주문.xlsx
│
├── rpi/                            # RPi 브릿지 (실제 하드웨어용)
├── mqtt_test.py                    # CLI 테스트 도구 (MQTT 기반)
├── FLOWCHART.md                    # 알고리즘 플로우차트 + 수정 이력
└── archive/                        # 이전 버전 (참조용, 수정 불필요)
```

---

## 시스템 구조

```
┌──────────────┐   MQTT           ┌──────────────────────────────┐    MQTT
│  mqtt_test   │ ──────────────>│          Server (PC)         │──────────────>  AGV (Webots)
│  (CLI 테스트) │  agv/algorithm  │                              │  /agv/plan       ├─ AGV-1 (홈=33)
└──────────────┘                 │  request_handler             │  /agv/shelf_cmd  └─ AGV-2 (홈=34)
                                 │  task_manager                │  /agv/control
                                 │  task_scheduler              │
                                 │  staging_manager             │<──────────────
                                 │  shelf_manager               │  /agv/arrived
                                 │  path_planner                │  /agv/shelf_ack
                                 │  robot_manager               │  /agv/marker
                                 └──────────────────────────────┘
```

### MQTT 토픽 전체 목록

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/control` | Server → AGV | resume 명령 (NODE_WAIT 해제) |
| `/agv/arrived` | AGV → Server | 최종 도착 / 중간 노드 위치 보고 |
| `/agv/shelf_ack` | AGV → Server | 리프트 완료 알림 |
| `/agv/marker` | AGV → Server | ArUco 마커 인식 (트리거 노드 통과) |
| `agv/algorithm` | GUI/CLI → Server | UI 명령 수신 |

---

## 맵 구조 (8×4 + 작업대 2개)

```
W1(33)── 1 ─ 2 ─ 3 ─ 4 ─ 5 ─ 6 ─ 7 ─ 8    (row 0, 통로)
         |   |   |   |   |   |   |   |
         9 ─10 ─[11]─[12]─13 ─[14]─[15]─16   (row 1, []=선반)
         |   |   |   |   |   |   |   |
        17 ─18 ─[19]─[20]─21 ─[22]─[23]─24   (row 2, []=선반)
         |   |   |   |   |   |   |   |
W2(34)──25 ─26 ─27 ─28 ─29 ─30 ─31 ─32    (row 3, 통로)

W1(33): gateway=1, staging=9, trigger=2
W2(34): gateway=25, staging=17, trigger=26
```

- **총 노드**: 34개 (32 그리드 + 2 작업대)
- **S (선반)**: 8개 — 11, 12, 14, 15, 19, 20, 22, 23
- **W (작업대)**: 2개 — 33(W1), 34(W2)

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
| `server/` | 서버 모듈 설명, API, 통신 프로토콜, 핵심 알고리즘 |
| `controllers/` | AGV 컨트롤러, NODE_WAIT, 리프트, 마커 |
| `worlds/` | 월드 파일, KIVA 선반, AGV 구조, ArUco |
| `config/` | 맵/선반/로봇 설정 |

알고리즘 플로우차트 및 수정 이력: `FLOWCHART.md`

---

## 필수 패키지

| 패키지 | 용도 |
|--------|------|
| `paho-mqtt` | MQTT 클라이언트 (서버↔AGV 통신) |
| `websockets` | WebSocket 서버 (Admin UI용) |
| `mosquitto` | MQTT 브로커 (시스템 패키지) |
| `pandas` | 엑셀 파일 읽기 (db_loader) |
| `openpyxl` | pandas의 xlsx 엔진 |
