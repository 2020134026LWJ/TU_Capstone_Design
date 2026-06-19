# TU_Capstone_Design

AGV 기반 물류 피킹 시스템 졸업작품

## 프로젝트 개요

**AGV(Automated Guided Vehicle)** 를 활용한 물류창고 피킹 자동화 시스템.
KIVA 스타일 이동식 선반을 AGV 2대가 작업대로 운반하여 작업 효율을 향상시킨다.

- **시뮬레이션**: Isaac Sim 5.1.0 (현재 메인) — Webots는 레거시
- **실물 제어**: STM32 펌웨어 + Raspberry Pi 브릿지 (UART)
- **통신**: MQTT (서버 ↔ AGV / GUI ↔ 서버) — cmd-based
- **인식**: ArUco 마커 (노드 ID = 마커 ID)

## 개발 목표

- AGV 기반 선반 이송 시스템 구현
- ArUco 마커 기반 경로 인식 및 주행
- 중앙 서버를 통한 경로 계획 / 작업 관리 / 충돌·교착 회피
- 시뮬레이션 ↔ 실물 코드 공통 추상화 (`hardware/` 레이어)

## 시스템 구성

```
+---------------+    MQTT          +-------------+      MQTT       +------------------+
| warehouse_gui | ---------------> |   Server    | --------------> | AGV (Isaac/RPi)  |
| (Kivy, 터치)  |  agv/algorithm   |  (Python)   |   /agv/cmd      |  Bridge + AGV    |
+---------------+                  +-------------+                 +---------+--------+
                                          ^                                  |
                                          |     /agv/marker                  |
                                          |     /agv/cmd_ack                 |
                                          +----------------------------------+
```

| 구성 요소 | 역할 | 기술 |
|-----------|------|------|
| AGV 서버 | 경로 계획, 작업 스케줄링, 충돌/교착 회피 | Python, MQTT, WebSocket |
| Isaac Sim | AGV + 창고 3D 시뮬레이션 | Isaac Sim 5.1.0, USD |
| AGV 실물 | 자율 이동 + 선반 리프트 | STM32 + Raspberry Pi 5 |
| warehouse_gui | 작업자 터치스크린 주문 GUI | Kivy, MQTT, Raspberry Pi |
| `hardware/` 추상화 | 시뮬/실물 공통 인터페이스 | Python (Bridge, Camera ABC) |

## 사용 기술

STM32 (CubeIDE) / Raspberry Pi 5 / Isaac Sim 5.1.0 / Webots (레거시) / Python / OpenCV (ArUco) / MQTT (paho-mqtt + Mosquitto) / WebSocket / UART

---

## 빠른 시작 (Isaac Sim 기반)

### 사전 요구사항

- Python 3.10+
- Isaac Sim 5.1.0 (소스 빌드 — `~/isaacsim/`)
- NVIDIA Driver 535.x 권장 (580 비호환)
- Mosquitto MQTT 브로커

### 1. 의존성 설치

```bash
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

# 터미널 2: Isaac Sim
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step6_visual.py

# 터미널 3: CLI 테스트
python3 mqtt_test.py
```

### CLI 명령어 (mqtt_test.py)

| 명령어 | 설명 |
|--------|------|
| `시작 1` | 사용자1 주문 시작 (W1=노드33) |
| `시작 2` | 사용자2 주문 시작 (W2=노드9) |
| `완료 1` | 사용자1 선반 피킹 완료 → 반납/포워딩 |
| `완료 2` | 사용자2 선반 피킹 완료 → 반납/포워딩 |
| `선반완료 1 [노드번호]` | 특정 선반 직접 지정 |
| `종료` | 종료 |

선반 노드 번호: `19(1-1) 20(1-2) 22(1-3) 23(1-4) 27(2-1) 28(2-2) 30(2-3) 31(2-4)`

### 테스트

```bash
pytest                          # 전체 회귀 (21 passed)
pytest -m "stg or deadlock"     # 특정 영역
```

---

## 디렉토리 구조

```
TU_Capstone_Design/
|
+-- server/                         # AGV 서버 (시뮬레이터 무관, MQTT 기반)
|   +-- main.py                     # 서버 진입점
|   +-- config.py                   # 설정 관리
|   +-- request_handler.py          # ★ 핵심: 요청 처리 + 로봇 배정 + 충돌/교착 알고리즘
|   +-- task_manager.py             # 작업 분해/스케줄링
|   +-- order_optimizer.py          # Nearest Neighbor 선반 방문 순서 최적화 (구 task_scheduler)
|   +-- robot_manager.py            # 로봇 6단계 상태 머신
|   +-- shelf_manager.py            # 선반 상태 관리 (IN_PLACE/CARRIED/AT_WORKSTATION)
|   +-- staging_manager.py          # 작업대 회랑 게이팅 (STG/TRG/위치 기반 해제)
|   +-- path_planner.py             # A* 시간 기반 경로 계획 (예약 기반 충돌 회피)
|   +-- mqtt_client.py              # MQTT 클라이언트 (publish + subscribe)
|   +-- websocket_handler.py        # WebSocket 서버 (Admin UI용, 선택적)
|   +-- db_loader.py                # 엑셀 DB 로더
|   +-- map.json                    # 8x6 그리드 맵 (48노드)
|   +-- shelf_config.json           # 선반/물품/작업대 설정 (8개 선반)
|   +-- robot_config.json           # 로봇 설정 (AGV-1 home=9(W2), AGV-2 home=33(W1))
|   +-- Database/                   # 주문 엑셀 데이터
|       +-- 데이터 베이스.xlsx
|       +-- 사용자1주문.xlsx
|       +-- 사용자2주문.xlsx
|
+-- isaac_simulation/               # Isaac Sim 5.1.0 시뮬레이션 (현재 메인)
|   +-- step6_visual.py             # 시각 개선 버전 (현재 메인 실행 파일)
|   +-- step7_kinematic.py          # Kinematic physics (작업 보류)
|   +-- STEP8_PLAN.md               # Articulation 전환 계획
|
+-- hardware/                       # 시뮬/실물 공통 추상화 + AGV 실물 코드
|   +-- bridge.py                   # MQTT <-> UART 브릿지 (Isaac Sim / RPi 공통)
|   +-- camera.py                   # RpiCamera / IsaacCamera 공통 인터페이스
|   +-- isaac_hw.py                 # IsaacMotors (시뮬레이터용 가상)
|   +-- rpi_main.py                 # RPi 진입점
|   +-- stm32/                      # STM32 펌웨어 (C)
|
+-- warehouse_gui_server/           # 작업자 터치스크린 GUI + 재고 서버
|   +-- warehouse_gui_v2.py         # KivyMD 작업자 UI (IP 입력 팝업 포함)
|   +-- warehouse_server_v2.py      # Flask 재고 API + MQTT (SQLite)
|   +-- excel_to_sqlite.py          # 데이터 베이스.xlsx → warehouse.db (최초 1회)
|   +-- 사용자{1,2}주문.xlsx        # 주문 데이터
|   +-- 데이터 베이스.xlsx          # 재고 마스터
|
+-- webots_simulation/              # Webots (레거시, 참조용)
|
+-- tests/                          # pytest 회귀 테스트 (21 passed)
|   +-- conftest.py                 # MockMqttPublisher + handler 픽스처
|   +-- test_smoke.py               # 픽스처 sanity
|   +-- test_collision.py           # 충돌 회피
|   +-- test_deadlock.py            # 교착 회피 (staging blocker 포함)
|   +-- test_intercept.py           # Node U 인터셉트
|   +-- test_stg.py                 # STG 게이팅
|
+-- FLOWCHART.md                    # 알고리즘 플로우차트 + 수정 이력 (설계 단일 진실)
+-- mqtt_test.py                    # CLI 테스트 도구 (MQTT 기반)
+-- archive/                        # 이전 버전 (참조용, 수정 불필요)
    +-- v1_prototype/
    +-- v2_single_file/
    +-- v3_modular_server/
```

## 버전 히스토리

| 버전 | 시기 | 위치 | 설명 |
|------|------|------|------|
| v1 | 01.13~14 | `archive/v1_prototype/` | 초기 프로토타입, 3×3 맵 |
| v2 | 01.20 | `archive/v2_single_file/` | 단일 파일, 9×5 맵, 다중 로봇 |
| v3 | 01.26 | `archive/v3_modular_server/` | 모듈화 서버, WebSocket, 9×5 맵 |
| v4 | 01.28~ | `server/` + `webots_simulation/` | 모듈화 서버 + Webots 검증 완료 |
| v5 | 03.~ | `+ isaac_simulation/` + `hardware/` | Isaac Sim 5.1.0 이전, cmd-based, 하드웨어 추상화 |

## v5 주요 기능

- **맵**: 8×6 그리드 + 작업대 2개 (총 48노드), 선반/통로/작업대 타입 구분
- **2대 AGV 동시 운영**: AGV-1 home=노드9(W2), AGV-2 home=노드33(W1)
- **cmd-based 통신**: forward/turn_*/lift_* 단일 명령 단위, ArUco 마커 도착 시 자동 종료
- **하드웨어 추상화**: `hardware/bridge.py` + `camera.py` ABC — Isaac Sim/RPi 코드 공유
- **선반 관리**: IN_PLACE/CARRIED/AT_WORKSTATION 상태 추적, 인터셉트(Node U) 지원
- **작업 관리**: 배치 작업, 물품→선반 매핑, 서브태스크 분해, Nearest Neighbor 최적화
- **포워딩**: 같은 선반이 두 작업대 모두 필요할 때 자동 포워딩
- **STG 게이팅**: 작업대 회랑 진입/퇴출 순서 관리 (staging 노드 분리 — 수정 28)
- **충돌 회피**: A* 시간 기반 경로 + `_reserved_nodes` 예약 기반 + cmd-based 차단/재시도
- **교착 회피**: 2단계 전략 (우회 경로 A* → 옆 노드 yield), staging blocker 안전망 포함 (수정 28)
- **엑셀 DB 연동**: 주문 데이터를 엑셀에서 로드

## 시스템 흐름 (cmd-based)

```
[mqtt_test/GUI] --MQTT(agv/algorithm)--> [Server] --MQTT(/agv/cmd)--> [AGV]
                                            ^                          |
                                            |  /agv/marker (마커 도착)  |
                                            |  /agv/cmd_ack (turn/lift) |
                                            +--------------------------+
```

상세 흐름:
1. GUI/CLI에서 `start_order` → 서버가 DB 로드 → 선반 방문 순서 최적화
2. 서버가 A*로 경로 계산 → 노드 경로를 cmd 시퀀스로 변환 → `/agv/cmd` 발행
3. AGV가 한 칸 이동 후 ArUco 마커 감지 → `/agv/marker` 발행 (마커 ID = 노드 ID)
4. 서버: 위치 갱신 + 충돌/교착 체크 + 다음 cmd 발행
5. 선반 노드 도착 → 서버가 `lift_up` 발행 → AGV 완료 후 `cmd_ack` → 작업대 이동 시작
6. 작업대 도착 → 작업자 피킹 → GUI에서 `shelf_complete` → 서버가 반납/포워딩 결정

## 문서

| 파일 | 내용 |
|------|------|
| [`FLOWCHART.md`](FLOWCHART.md) | 알고리즘 플로우차트 + 수정 이력 (설계 단일 진실) |
| [`server/README.md`](server/README.md) | 서버 진입점 안내 + 모듈 구조 |
| [`server/docs/DISPATCH_FLOW.md`](server/docs/DISPATCH_FLOW.md) | 주문→cmd 발행 디스패치 흐름 (한글) |
| [`server/docs/REFACTOR_F.md`](server/docs/REFACTOR_F.md) | 경로/예약 재설계(REFACTOR F) 내역 |
| [`isaac_simulation/README.md`](isaac_simulation/README.md) | Isaac Sim 5.1.0 시뮬레이션 상세 |
| [`hardware/README.md`](hardware/README.md) | Bridge + Camera ABC + UART 프로토콜 |
| [`webots_simulation/README.md`](webots_simulation/README.md) | Webots (레거시) 참조 |
| [`warehouse_gui_server/Readme.md`](warehouse_gui_server/Readme.md) | 작업자 GUI + 재고 서버 |
