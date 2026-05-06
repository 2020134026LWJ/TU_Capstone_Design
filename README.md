# TU_Capstone_Design
AGV 기반 물류 피킹 시스템 졸업작품

## 프로젝트 개요
본 프로젝트는 **AGV(Automated Guided Vehicle)** 를 활용한
물류창고 피킹 자동화 시스템을 설계·구현하는 졸업작품이다.
KIVA 스타일 이동식 선반을 AGV가 자동으로 운반하여 작업 효율을 향상시키는 것을 목표로 한다.

## 개발 목표
- AGV 기반 선반 이송 시스템 구현
- ArUco 마커 기반 경로 인식 및 주행
- 중앙 서버를 통한 경로 제어 및 작업 관리
- STM32 + Raspberry Pi 기반 제어 시스템 설계

## 시스템 구성
- **AGV 제어부**: STM32 (모터, 센서, 리니어 액추에이터 제어)
- **상위 제어부**: Raspberry Pi 5 (경로 수신, 마커 인식)
- **중앙 서버**: 경로 계산, 작업 관리, 선반 관리
- **통신**: MQTT (서버↔AGV / CLI↔서버) / WebSocket (서버↔Admin UI)
- **시뮬레이션**: Isaac Sim 5.1.0 (AGV 2대 + KIVA 선반 8개, 8×6 그리드) / Webots (완료, 레거시)

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
cd TU_Capstone_Design
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
webots webots_simulation/worlds/warehouse_4x8.wbt
```

### 6. CLI 테스트 도구 실행 (터미널 3)
```bash
python3 mqtt_test.py
```

### CLI 명령어

| 명령어 | 설명 |
|--------|------|
| `시작 1` | 사용자1 주문 시작 (AGV-1, W1) |
| `시작 2` | 사용자2 주문 시작 (AGV-2, W2) |
| `완료 1` | 사용자1 선반 피킹 완료 → 반납/포워딩 |
| `완료 2` | 사용자2 선반 피킹 완료 → 반납/포워딩 |
| `선반완료 1 [노드번호]` | 특정 선반 피킹 완료 직접 지정 |
| `종료` | 프로그램 종료 |

선반 노드 번호: `19(1-1) 20(1-2) 22(1-3) 23(1-4) 27(2-1) 28(2-2) 30(2-3) 31(2-4)`

---

## 디렉토리 구조

```
TU_Capstone_Design/
│
├── server/                         # AGV 서버 (시뮬레이터 무관, MQTT 기반)
│   ├── main.py                     # 서버 진입점
│   ├── config.py                   # 설정 관리
│   ├── request_handler.py          # ★ 핵심: 요청 처리 + 로봇 배정 알고리즘
│   ├── task_manager.py             # 작업 분해/스케줄링
│   ├── task_scheduler.py           # Nearest Neighbor 선반 방문 순서 최적화
│   ├── robot_manager.py            # 로봇 6단계 상태 머신
│   ├── shelf_manager.py            # 선반 상태 관리 (IN_PLACE/CARRIED/AT_WORKSTATION)
│   ├── staging_manager.py          # 작업대 회랑 게이팅 (STG/TRG/위치 기반 해제)
│   ├── path_planner.py             # A* 시간 기반 경로 계획 (예약 기반 충돌 회피)
│   ├── mqtt_publisher.py           # MQTT 발행
│   ├── websocket_handler.py        # WebSocket 서버 (Admin UI용)
│   ├── db_loader.py                # 엑셀 DB 로더
│   ├── map.json                    # 8×4 그리드 맵 (34노드)
│   ├── shelf_config.json           # 선반/물품/작업대 설정 (8개 선반)
│   ├── robot_config.json           # 로봇 설정 (AGV-1 home=9(W2), AGV-2 home=33(W1))
│   └── Database/                   # 주문 엑셀 데이터
│       ├── 데이터 베이스.xlsx
│       ├── 사용자1주문.xlsx
│       └── 사용자2주문.xlsx
│
├── hardware/                       # 실물 AGV 하드웨어 코드
│   ├── stm32/                      # STM32 펌웨어 (C)
│   └── rpi/                        # Raspberry Pi 브릿지 (Python)
│
├── webots_simulation/              # Webots 시뮬레이션 전용
│   ├── controllers/                # Webots 컨트롤러
│   │   ├── agv_controller/         # 기본 테스트용 (MQTT 없이 단독 실행)
│   │   └── agv_mqtt_controller/    # MQTT + Supervisor + 리프트 (현재 사용)
│   │       ├── main.py
│   │       ├── agv_controller.py   # 메인 AGV 로직 (마커감지, 리프트, NODE_WAIT)
│   │       ├── aruco_detector.py
│   │       ├── mqtt_handler.py
│   │       ├── navigation.py
│   │       └── hardware/
│   ├── worlds/
│   │   └── warehouse_4x8.wbt       # 현재 월드 (8×4, KIVA 선반 8개, ArUco 마커)
│   └── textures/aruco_markers/     # ArUco 마커 이미지
│
├── isaac_simulation/               # Isaac Sim 5.1.0 시뮬레이션 전용
├── warehouse_gui_server/           # 작업자 터치스크린 GUI + 재고 서버
├── FLOWCHART.md                    # 알고리즘 플로우차트 + 수정 이력
├── mqtt_test.py                    # CLI 테스트 도구 (MQTT 기반)
├── archive/                        # 이전 버전 아카이브 (참조용, 수정 불필요)
│   ├── v1_prototype/               # 초기 프로토타입 (01.13~14, 3×3 맵)
│   ├── v2_single_file/             # 단일 파일 서버 (01.20, 9×5 맵)
│   └── v3_modular_server/          # 모듈화 서버 (01.26, 9×5 맵)
└── docs/
```

## 버전 히스토리

| 버전 | 날짜 | 위치 | 설명 |
|------|------|------|------|
| v1 | 01.13~14 | `archive/v1_prototype/` | 초기 프로토타입, 3×3 맵 |
| v2 | 01.20 | `archive/v2_single_file/` | 단일 파일, 9×5 맵, 다중 로봇 |
| v3 | 01.26 | `archive/v3_modular_server/` | 모듈화 서버, WebSocket, 9×5 맵 |
| v4 | 01.28~ | `webots_simulation/` | 8×4 맵, 선반/작업 관리, KIVA 선반, NODE_WAIT |

## v4 주요 기능

- **맵**: 8×6 그리드 + 작업대 2개 (총 48노드), 선반/통로/작업대 타입 구분
- **KIVA 3D 선반**: 선반 8개 (4다리 + 선반판 구조), 3~4종 물품/선반
- **AGV 리프트**: SliderJoint + LinearMotor 기반 리프트 메커니즘
- **Supervisor API**: 선반 들어올리기/내려놓기 시 3D 좌표 동기화
- **ArUco 마커**: 작업대 트리거 노드 통과 감지 → 스테이징 해제
- **NODE_WAIT**: 서버 기반 노드 단위 교착 방지 (중간 노드 정지 + resume)
- **선반 관리**: 8개 선반, IN_PLACE/CARRIED/AT_WORKSTATION 상태 추적
- **작업 관리**: 배치 작업 등록, 물품→선반 자동 매핑, 서브태스크 분해
- **작업 최적화**: Nearest Neighbor 알고리즘으로 선반 방문 순서 최적화
- **포워딩**: 같은 선반이 여러 작업대에서 필요할 때 자동 포워딩
- **STG 게이팅**: 작업대 회랑 진입/퇴출 순서 관리 (교착 방지)
- **경로 계획**: A* (시간 기반 충돌 회피, 선반 노드 통과 제외)
- **2대 AGV 동시 운영**: W1(노드33, AGV-2), W2(노드9, AGV-1) 각각 담당
- **엑셀 DB 연동**: 주문 데이터를 엑셀 파일에서 로드

## 시스템 흐름

```
[mqtt_test.py] ──MQTT(agv/algorithm)──> [Server] ──MQTT──> [AGV (Webots)]
                                           │                    │
                                           │     /agv/plan      │ 경로 직접 수신
                                           │  /agv/shelf_cmd    │ 선반 리프트 명령
                                           │   /agv/control     │ resume 명령
                                           │                    │
                                           │   /agv/arrived     │ 도착/위치 보고
                                           │  /agv/shelf_ack    │ 리프트 완료
                                           │    /agv/marker     │ ArUco 트리거
```
