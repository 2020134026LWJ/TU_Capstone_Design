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
- **AGV 제어부**: STM32 (모터, 센서, 액추에이터 제어)
- **상위 제어부**: Raspberry Pi (경로 수신, 마커 인식)
- **중앙 서버**: 경로 계산 및 작업 관리
- **통신**: MQTT / WebSocket

## 사용 기술
- STM32 (CubeIDE)
- Raspberry Pi 5
- Webots (시뮬레이션)
- Python
- OpenCV (ArUco Marker)
- MQTT / WebSocket
- Git / GitHub

## 디렉토리 구조

TU_Capstone_Design/
│
├── webots_simulation/          # [메인] Webots 시뮬레이션 프로젝트
│   ├── server/                 # 모듈화된 AGV 서버 (v3)
│   │   ├── main.py             # 서버 진입점
│   │   ├── config.py           # 설정 관리
│   │   ├── path_planner.py     # A* 경로 계획
│   │   ├── mqtt_publisher.py   # MQTT 발행
│   │   ├── websocket_handler.py# WebSocket 서버
│   │   ├── request_handler.py  # 요청 처리
│   │   ├── robot_manager.py    # 로봇 상태 관리
│   │   └── README.md           # 서버 설계 문서
│   │
│   ├── controllers/            # Webots 컨트롤러
│   ├── worlds/                 # Webots 월드 파일
│   ├── bridge.py               # MQTT 브릿지
│   ├── map.json                # 9x5 그리드 맵
│   └── robot_config.json       # 로봇 설정
│
├── archive/                    # 이전 버전 아카이브
│   ├── v1_prototype/           # 초기 프로토타입 (01.13~14)
│   └── v2_single_file/         # 단일 파일 서버 (01.20)
│
├── docs/                       # 문서
│   ├── 종합설계기획/
│   └── 진백이조 공금 관련 문서/
│
├── samples/                    # 참고용 샘플
│   └── admin_ui_html/          # 관리자 UI HTML 샘플
│
└── README.md


## 버전 히스토리

| 버전 | 날짜 | 위치 | 설명 |
|------|------|------|------|
| v1 | 01.13~14 | `archive/v1_prototype/` | 초기 프로토타입, 3x3 맵 |
| v2 | 01.20 | `archive/v2_single_file/` | 단일 파일, 9x5 맵, 다중 로봇 |
| v3 | 01.26 | `webots_simulation/server/` | 모듈화 서버, WebSocket 지원 |


## 실행 방법

### 1. 서버 실행
cd webots_simulation
python -m server.main

### 2. 브릿지 실행
cd webots_simulation
python bridge.py

### 3. Webots 시뮬레이션 실행
# Webots에서 worlds/warehouse.wbt 열기


## 시스템 흐름

[Admin UI] --WebSocket--> [Server] --MQTT--> [bridge.py] --MQTT--> [AGV]
     │                       │
     │                       ├── 요청 수신 (작업자 ID, 선반 마커)
     │                       ├── 마커 → 노드 변환
     │                       ├── A* 경로 계획
     │                       └── MQTT 발행
     │
     └── 응답 수신 (경로, 상태)