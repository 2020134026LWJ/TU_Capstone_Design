# TU_Capstone_Design
AGV 기반 물류 피킹 시스템 졸업작품

## 프로젝트 개요
본 프로젝트는 **AGV(Automated Guided Vehicle)** 를 활용한
물류창고 피킹 자동화 시스템을 설계·구현하는 졸업작품이다.
이동식 선반을 AGV가 자동으로 운반하여 작업 효율을 향상시키는 것을 목표로 한다.

## 개발 목표
- AGV 기반 선반 이송 시스템 구현
- 아루코 마커 기반 경로 인식 및 주행
- 중앙 서버를 통한 경로 제어 및 작업 관리
- STM32 + Raspberry Pi 기반 제어 시스템 설계

## 시스템 구성
- **AGV 제어부**: STM32 (모터, 센서, 리니어 액추에이터 제어)
- **상위 제어부**: Raspberry Pi 5 (경로 수신, 마커 인식, Bridge)
- **중앙 서버**: 경로 계산, 작업 관리, 선반 관리
- **통신**: MQTT (서버↔RPi) / UART (RPi↔STM32) / WebSocket (서버↔Admin UI)

## 사용 기술
- STM32 (CubeIDE)
- Raspberry Pi 5
- Webots (시뮬레이션)
- Python
- OpenCV (ArUco Marker)
- MQTT / WebSocket / UART
- Git / GitHub

## 디렉토리 구조

```
TU_Capstone_Design/
│
├── webots_simulation/              # [메인] Webots 시뮬레이션 프로젝트 (v4)
│   ├── server/                     # 모듈화된 AGV 서버
│   │   ├── main.py                 # 서버 진입점
│   │   ├── config.py               # 설정 관리
│   │   ├── path_planner.py         # A* 경로 계획 (선반 통과 제외)
│   │   ├── mqtt_publisher.py       # MQTT 발행
│   │   ├── websocket_handler.py    # WebSocket 서버
│   │   ├── request_handler.py      # 요청 처리 (배치작업, 픽완료, 도착)
│   │   ├── robot_manager.py        # 로봇 상태 관리 (6단계 상태머신)
│   │   ├── shelf_manager.py        # 선반 상태 관리 (위치, 물품, 운반)
│   │   └── task_manager.py         # 작업 분해 및 스케줄링
│   │
│   ├── controllers/                # Webots 컨트롤러
│   ├── worlds/                     # Webots 월드 파일
│   ├── bridge.py                   # 양방향 브릿지 (MQTT ↔ UART)
│   ├── map.json                    # 7×7 그리드 맵 (51노드)
│   ├── shelf_config.json           # 선반별 물품 매핑
│   └── robot_config.json           # 로봇 설정
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
| v1 | 01.13~14 | `archive/v1_prototype/` | 초기 프로토타입, 3×3 맵 |
| v2 | 01.20 | `archive/v2_single_file/` | 단일 파일, 9×5 맵, 다중 로봇 |
| v3 | 01.26 | `archive/v3_modular_server/` | 모듈화 서버, WebSocket, 9×5 맵 |
| v4 | 01.28 | `webots_simulation/` | 7×7 맵, 선반/작업 관리, UART 프로토콜 |

## v4 주요 변경사항

- **맵**: 9×5(45노드) → 7×7+작업대2(51노드), 선반/통로/작업대 타입 구분
- **선반 관리**: 9개 선반 × 3종 물품, 선반 상태 추적 (제자리/운반중/작업대)
- **작업 관리**: 배치 작업 등록, 물품→선반 자동 매핑, 서브태스크 분해
- **작업 흐름**: 선반이동→리프트→배달→픽업대기→복귀 (포워딩 지원)
- **로봇 상태**: IDLE→MOVING→PICKUP→DELIVER→WAIT→RETURN 6단계
- **경로 계획**: 선반 노드 통과 제외 (출발/도착만 허용)
- **브릿지**: MQTT↔UART 양방향 중계, STM32 바이너리 패킷 프로토콜

## 시스템 흐름

```
[Admin UI] ──WebSocket──> [Server] ──MQTT──> [Bridge(RPi)] ──UART──> [STM32]
                             │                    │
                             │                    ├─ MOVE_TO_NODE (이동)
                             │                    ├─ LIFT_UP/DOWN (리프트)
                             │                    │
                             │                    ├─ MOVE_DONE → /agv/arrived
                             │                    └─ LIFT_DONE → /agv/state
                             │
                             ├─ task_manager: 작업 분해/스케줄링
                             ├─ shelf_manager: 선반 상태 추적
                             ├─ path_planner: A* 경로 계획
                             └─ robot_manager: 로봇 상태 관리
```

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
