# TU_Capstone_Design
AGV 기반 물류 피킹 시스템 졸업작품

## 프로젝트 개요
본 프로젝트는 **AGV(Automated Guided Vehicle)** 를 활용한
물류창고 피킹 자동화 시스템을 설계·구현하는 졸업작품이다.
KIVA 스타일 이동식 선반을 AGV가 자동으로 운반하여 작업 효율을 향상시키는 것을 목표로 한다.

## 개발 목표
- AGV 기반 선반 이송 시스템 구현
- 아루코 마커 기반 경로 인식 및 주행
- 중앙 서버를 통한 경로 제어 및 작업 관리
- STM32 + Raspberry Pi 기반 제어 시스템 설계

## 시스템 구성
- **AGV 제어부**: STM32 (모터, 센서, 리니어 액추에이터 제어)
- **상위 제어부**: Raspberry Pi 5 (경로 수신, 마커 인식)
- **중앙 서버**: 경로 계산, 작업 관리, 선반 관리
- **통신**: MQTT (서버↔AGV) / WebSocket (서버↔CLI/Admin UI)
- **시뮬레이션**: Webots (AGV 2대 + KIVA 선반 9개)

## 사용 기술
- STM32 (CubeIDE)
- Raspberry Pi 5
- Webots (시뮬레이션)
- Python
- OpenCV (ArUco Marker)
- MQTT / WebSocket / UART
- Git / GitHub

---

## 시뮬레이션 테스트 방법

### 사전 요구사항
- Python 3.10+
- Webots R2023b 이상
- Mosquitto MQTT 브로커

### 1. 프로젝트 클론
```bash
git clone git@github.com:2020134026LWJ/TU_Capstone_Design.git
cd TU_Capstone_Design/webots_simulation
```

### 2. 의존성 설치
```bash
pip install -r server/requirements.txt
```

### 3. MQTT 브로커 실행
```bash
sudo apt install mosquitto
sudo systemctl start mosquitto
```

### 4. 서버 실행 (터미널 1)
```bash
python3 -m server.main
```

### 5. Webots 시뮬레이션 실행 (터미널 2)
```bash
webots worlds/warehouse_7x7.wbt
```

### 6. CLI 테스트 도구 실행 (터미널 3)
```bash
python3 websocket_test.py
```

### CLI 명령어

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
TU_Capstone_Design/
│
├── webots_simulation/              # [메인] Webots 시뮬레이션 프로젝트 (v4)
│   ├── server/                     # 모듈화된 AGV 서버
│   │   ├── main.py                 # 서버 진입점
│   │   ├── config.py               # 설정 관리
│   │   ├── path_planner.py         # Prioritized A* 경로 계획
│   │   ├── mqtt_publisher.py       # MQTT 발행
│   │   ├── websocket_handler.py    # WebSocket 서버
│   │   ├── request_handler.py      # 요청 처리 (주문, 픽완료, 도착, shelf_ack)
│   │   ├── robot_manager.py        # 로봇 상태 관리 (6단계 상태머신)
│   │   ├── shelf_manager.py        # 선반 상태 관리 (위치, 물품, 운반)
│   │   ├── task_manager.py         # 작업 분해 및 스케줄링
│   │   ├── task_scheduler.py       # Nearest Neighbor 작업 최적화
│   │   └── db_loader.py            # 엑셀 DB 로더 (주문 데이터)
│   │
│   ├── controllers/                # Webots 컨트롤러
│   │   ├── agv_controller/         # 기본 테스트용
│   │   └── agv_mqtt_controller/    # MQTT + Supervisor + 리프트 제어
│   │
│   ├── config/                     # 설정 파일
│   │   ├── map.json                # 7x7 그리드 맵 (51노드)
│   │   ├── shelf_config.json       # 선반별 물품 매핑
│   │   └── robot_config.json       # 로봇 설정
│   │
│   ├── worlds/                     # Webots 월드 파일
│   │   └── warehouse_7x7.wbt      # 현재 월드 (7x7 + KIVA 선반)
│   │
│   ├── Database/                   # 주문 엑셀 데이터
│   │   ├── 데이터 베이스.xlsx
│   │   ├── 사용자1주문.xlsx
│   │   └── 사용자2주문.xlsx
│   │
│   ├── rpi/                        # RPi 브릿지 (실제 하드웨어용)
│   ├── websocket_test.py           # CLI 테스트 도구
│   ├── test_forward.py             # 포워딩 테스트
│   └── test_dual_order.py          # 듀얼 주문 테스트
│
├── archive/                        # 이전 버전 아카이브
│   ├── v1_prototype/               # 초기 프로토타입 (01.13~14)
│   ├── v2_single_file/             # 단일 파일 서버 (01.20)
│   └── v3_modular_server/          # 모듈화 서버 (01.26)
│
├── docs/                           # 문서
│   ├── 종합설계기획/
│   └── 진백이조 공금 관련 문서/
│
├── samples/                        # 참고용 샘플
│   └── admin_ui_html/              # 관리자 UI HTML 샘플
│
└── README.md
```

## 버전 히스토리

| 버전 | 날짜 | 위치 | 설명 |
|------|------|------|------|
| v1 | 01.13~14 | `archive/v1_prototype/` | 초기 프로토타입, 3x3 맵 |
| v2 | 01.20 | `archive/v2_single_file/` | 단일 파일, 9x5 맵, 다중 로봇 |
| v3 | 01.26 | `archive/v3_modular_server/` | 모듈화 서버, WebSocket, 9x5 맵 |
| v4 | 01.28~ | `webots_simulation/` | 7x7 맵, 선반/작업 관리, KIVA 선반, 리프트 |

## v4 주요 기능

- **맵**: 7x7 그리드 + 작업대 2개 (총 51노드), 선반/통로/작업대 타입 구분
- **KIVA 3D 선반**: 3층 선반 9개 (4다리 + 3선반판 구조)
- **AGV 리프트**: SliderJoint + LinearMotor 기반 리프트 메커니즘
- **Supervisor API**: 선반 들어올리기/내려놓기 시 3D 좌표 동기화
- **선반 관리**: 9개 선반 x 3종 물품, 선반 상태 추적 (제자리/운반중/작업대)
- **작업 관리**: 배치 작업 등록, 물품→선반 자동 매핑, 서브태스크 분해
- **작업 최적화**: Nearest Neighbor 알고리즘으로 선반 방문 순서 최적화
- **포워딩**: 같은 선반이 여러 작업대에서 필요할 때 자동 포워딩
- **경로 계획**: Prioritized A* (시간 기반 충돌 회피, 선반 노드 통과 제외)
- **2대 AGV 동시 운영**: 각각 W1/W2 작업대 담당
- **엑셀 DB 연동**: 주문 데이터를 엑셀 파일에서 로드

## 시스템 흐름

```
[CLI / Admin UI] ──WebSocket──> [Server] ──MQTT──> [AGV (Webots)]
                                   │                    │
                                   │        /agv/plan   │ 경로 직접 수신
                                   │     /agv/shelf_cmd │ 선반 리프트 명령
                                   │                    │
                                   │        /agv/arrived │ 도착 알림
                                   │      /agv/shelf_ack│ 리프트 완료 알림
                                   │                    │
                                   ├─ task_manager     : 작업 분해/스케줄링
                                   ├─ task_scheduler   : Nearest Neighbor 최적화
                                   ├─ shelf_manager    : 선반 상태 추적
                                   ├─ path_planner     : Prioritized A* 경로 계획
                                   ├─ robot_manager    : 로봇 상태 관리
                                   └─ db_loader        : 엑셀 DB 로더
```

## 실행 방법

```bash
# 터미널 1: 서버
cd webots_simulation
python3 -m server.main

# 터미널 2: Webots 시뮬레이션
webots worlds/warehouse_7x7.wbt

# 터미널 3: CLI 테스트
python3 websocket_test.py
```
