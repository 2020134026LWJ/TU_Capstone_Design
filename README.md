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
- 시뮬/실물 분리 코드 구조 (Isaac=`isaac_simulation/`, 실물=`hardware/`)

## 시스템 구성

```
+---------------+    MQTT          +-------------+      MQTT       +------------------+
| warehouse_gui | ---------------> |   Server    | --------------> | AGV (Isaac/RPi)  |
| (Kivy, 터치)  | warehouse/order/*|  (Python)   |   /agv/cmd      |  Bridge + AGV    |
+---------------+                  +-------------+                 +---------+--------+
                                          ^                                  |
                                          |     /agv/marker                  |
                                          |     /agv/cmd_ack                 |
                                          +----------------------------------+
```

| 구성 요소 | 역할 | 기술 |
|-----------|------|------|
| AGV 서버 | 경로 계획, 작업 스케줄링, 충돌/교착 회피 | Python, MQTT |
| Isaac Sim | AGV + 창고 3D 시뮬레이션 | Isaac Sim 5.1.0, USD |
| AGV 실물 | 자율 이동 + 선반 리프트 | STM32 + Raspberry Pi 5 |
| warehouse_gui | 작업자 터치스크린 주문 GUI | Kivy, MQTT, Raspberry Pi |
| `hardware/` (실물) | 실물 RPi bridge + 카메라 + config | Python (bridge_rpi, camera, config) |

## 사용 기술

STM32 (CubeIDE) / Raspberry Pi 5 / Isaac Sim 5.1.0 / Webots (레거시) / Python / OpenCV (ArUco) / MQTT (paho-mqtt + Mosquitto) / UART

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

# 터미널 2: Isaac Sim (AGV 역할 — 시뮬 단독 실행)
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py

# 터미널 3: 주문 넣기 — 라파 GUI (warehouse_gui_ws1.py / warehouse_gui_ws2.py)
#   또는 직접 발행:
mosquitto_pub -h localhost -t warehouse/order/start \
  -m '{"사용자ID":1,"주문번호":1,"작업대":2}'
```

> **Isaac을 실물/벤치의 디지털 트윈으로 띄우려면** `./isaac_simulation/run_twin.sh` (TWIN=1).
> TWIN 없이 띄우면 Isaac이 트윈이 아니라 **AGV 본인**이 되어 마커를 직접 발행한다 —
> 실물과 동시에 켜면 발행자가 둘이 되어 서버 상태가 꼬인다.

> 구 CLI 도구 `mqtt_test.py`는 GUI가 MQTT로 완전 전환되면서 `archive/`로 이동했다 (2026-06-19).

선반 노드 번호: `18(1-1) 19(1-2) 26(1-3) 27(1-4) 21(2-1) 22(2-2) 29(2-3) 30(2-4)`
(라벨↔노드 매핑의 단일 진실 = `server/data/shelf_config.json`. 코드는 여기서 읽는다 — 하드코딩 없음)

### 실물 없이 돌려보기 / 실물 붙이기

| 하고 싶은 것 | 문서 |
|---|---|
| 로봇 없이 책상에서 전체 루프 돌리기 (벤치 하네스) | [`virtual_test/README.md`](virtual_test/README.md) |
| 실물 AGV 처음 붙이는 날 (HIL bring-up 절차) | [`hardware/INTEGRATION.md`](hardware/INTEGRATION.md) |

### 테스트

```bash
pytest                          # 알고리즘 회귀 (100 tests)
pytest -m "stg or deadlock"     # 특정 영역
```

---

## 디렉토리 구조

```
TU_Capstone_Design/
|
+-- server/                         # AGV 서버 (시뮬레이터 무관, MQTT 기반) — 2026-06-13 레이어별 분리
|   +-- main.py  config.py  __init__.py
|   +-- comm/                       # mqtt_client.py (publish+subscribe)
|   +-- core/                       # ★ 핵심 알고리즘: request_handler.py + _movement/_marker/_workflow_mixin.py
|   +-- planning/                   # path_planner / reservation_service / deadlock_detector / order_optimizer / command_queue
|   +-- managers/                   # robot.py / shelf.py / task.py / staging.py (도메인 상태)
|   +-- data/                       # db_loader.py + map.json / shelf_config.json / robot_config.json
|   +-- docs/                       # README.md / DISPATCH_FLOW.md / REFACTOR_F.md
|       # 주문 엑셀은 warehouse_gui_server/ 로 통합 (구 server/Database/는 archive)
|
+-- isaac_simulation/               # Isaac Sim 5.1.0 (현재 메인) — Isaac 전용
|   +-- step7_kinematic.py          # ★ 현재 메인 (Kinematic Physics + 디지털 트윈 TWIN=1)
|   +-- step6_visual.py             # 시각 전용 버전 (물리 없음, 트윈 미지원)
|   +-- run_twin.sh                 # 트윈 모드 실행 래퍼 (TWIN=1 + 칸당 소요시간)
|   +-- bridge_isaac.py             # Isaac 전용 bridge (콜백) / isaac_hw.py / camera.py(IsaacCamera)
|   +-- capture_agv.py              # 발표 자료용 정적 포즈 촬영 (MQTT 없음)
|   +-- Presentation_manual.py      # 발표용 수동 제어 데모 (서버 없이 노드 지정)
|   +-- STEP8_PLAN.md               # Articulation 전환 계획 (졸업 후)
|
+-- hardware/                       # AGV 실물(RPi) 전용
|   +-- INTEGRATION.md              # ★ 실물 붙이는 날 따라가는 bring-up 절차서
|   +-- README.md                   # 구조·프로토콜·환경구축 레퍼런스
|   +-- rpi_main.py                 # 실물 진입점 (camera+bridge, AGV_ID로 rid)
|   +-- bridge_rpi.py               # MQTT <-> UART 브릿지 (주원이 STM ASCII 프로토콜)
|   +-- camera.py                   # RpiCamera (주원이 opencv ArUco 비전)
|   +-- camera_preview.py           # 초점 맞추기용 웹 프리뷰 (라파에 모니터 없어도 됨)
|   +-- config.py                   # 배포 설정 (서버IP/UART/카메라/HEADING_OFFSET)
|   +-- stm32/rpi_uart.c            # 주원이 STM UART 소스 / AGV_Control.zip (펌웨어 전체)
|
+-- warehouse_gui_server/           # 작업자 터치스크린 GUI + 재고 서버 (협업자 공유)
|   +-- warehouse_gui_ws1.py        # 1번 라파(작업대1) UI / warehouse_gui_ws2.py = 2번 라파
|   |                               #   [주의] warehouse_gui_v2.py는 ws2 사본(WORKSTATION_ID=2).
|   |                               #   1번 라파에서 열면 주문이 작업대 2로 발행된다
|   +-- warehouse_server_v2.py      # Flask 재고 API + MQTT (SQLite)
|   +-- excel_to_sqlite.py          # 데이터 베이스.xlsx → warehouse.db (최초 1회)
|   +-- 사용자{1,2}주문.xlsx        # 주문 데이터
|   +-- 데이터 베이스.xlsx          # 재고 마스터
|
+-- (webots는 archive/webots_simulation_old/ 로 이동 — 레거시)
|
+-- virtual_test/                   # 실물 없이 도는 것 전부 (README.md에 3모드 설명)
|   +-- algorithm/                  # pytest 알고리즘 회귀 (100 tests, 구 tests/)
|   |   +-- conftest.py             # MockMqttPublisher + handler 픽스처
|   |   +-- test_*.py               # 충돌/교착/인터셉트/STG/예약/멱등 등
|   +-- bench_camera/run_bench.py   # 벤치 하네스 — 가짜 로봇(--auto-walk) 또는 손마커
|   +-- software_in_the_loop/       # SIL — 가짜 STM + 가상 UART(pty) 통신 검증
|       +-- mock_stm.py  run_sil.py
|
+-- FLOWCHART.md                    # 알고리즘 플로우차트 + 수정 이력 (설계 단일 진실)
+-- archive/                        # 이전 버전 + 폐기 도구 (참조용, 수정 불필요)
    +-- v1_prototype/ v2_single_file/ v3_modular_server/
    +-- webots_simulation_old/      # Webots (레거시)
    +-- mqtt_test.py                # 구 CLI 주문 도구 (GUI 전환으로 폐기)
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

- **맵**: 8×6 그리드 (노드 0~47) + 작업대 2개 + 입고 스테이션(47번), 선반/통로/작업대 타입 구분
- **2대 AGV 동시 운영**: AGV-1 home=노드8(W2), AGV-2 home=노드32(W1) — 교착 회피를 위한 스왑 배치
- **cmd-based 통신**: forward/turn_*/lift_* 단일 명령 단위, ArUco 마커 도착 시 자동 종료
- **시뮬/실물 분리**: Isaac=`isaac_simulation/`(bridge_isaac, IsaacCamera), 실물=`hardware/`(bridge_rpi, RpiCamera, config) — 주원이 STM 프로토콜 대응
- **디지털 트윈**: 실물/벤치가 발행한 마커를 Isaac이 따라 그림 (`run_twin.sh`, 회전까지 실시간 추종)
- **선반 관리**: IN_PLACE/CARRIED/AT_WORKSTATION 상태 추적, 인터셉트(Node U) 지원
- **작업 관리**: 주문 → 선반별 태스크 분해, 물품→선반 매핑, Nearest Neighbor 방문순서 최적화
- **포워딩**: 같은 선반이 두 작업대 모두 필요할 때 자동 포워딩
- **STG 게이팅**: 작업대 회랑 진입/퇴출 순서 관리 (staging 노드 분리 — 수정 28)
- **충돌 회피**: 노드 락(통행권) 모델 — `ReservationService`가 미래 점유의 단일 진실 (수정 55)
- **교착 회피**: 우회 A* → 옆 노드 yield 2단계 + wait-for 사이클 감지(`deadlock_detector`, 수정 54)
- **presence**: MQTT LWT로 AGV 접속/이탈 추적 — 안 켠 로봇에는 태스크를 주지 않는다 (수정 75)
- **마커 오검출 방어**: 인접성 검사 + forward 목표 일치 검사 → "순간이동" 차단 (수정 62/64)
- **엑셀 DB 연동**: 주문 데이터를 엑셀에서 로드 (`warehouse_gui_server/` 공유)

## 시스템 흐름 (cmd-based)

```
[GUI] --MQTT(warehouse/order/*)--> [Server] --MQTT(/agv/cmd)--> [AGV]
                                  ^                            |
                                  |  /agv/marker   (마커 도착) |
                                  |  /agv/cmd_ack  (turn/lift) |
                                  |  /agv/presence (접속/이탈) |
                                  +----------------------------+
```

상세 흐름:
1. GUI에서 `start_order` → 서버가 DB 로드 → 선반 방문 순서 최적화
2. 서버가 A*로 경로 계산 → 노드 경로를 cmd 시퀀스로 변환 → `/agv/cmd` 발행
3. AGV가 한 칸 이동 후 ArUco 마커 감지 → `/agv/marker` 발행 (마커 ID = 노드 ID)
4. 서버: 위치 갱신 + 충돌/교착 체크 + 다음 cmd 발행
5. 선반 노드 도착 → 서버가 `lift_up` 발행 → AGV 완료 후 `cmd_ack` → 작업대 이동 시작
6. 작업대 도착 → 작업자 피킹 → GUI에서 `shelf_complete` → 서버가 반납/포워딩 결정

## 문서

| 파일 | 내용 |
|------|------|
| [`FLOWCHART.md`](FLOWCHART.md) | 알고리즘 플로우차트 + 수정 이력 (설계 단일 진실) |
| [`../설계_근본해결_노트.md`](../설계_근본해결_노트.md) | 왜 이 구조인가 — 노드 락(통행권) 모델 (평문). **저장소 밖 상위 폴더** |
| [`../용어사전_쉽게.md`](../용어사전_쉽게.md) | STG/TRG/인터셉트/예약 등 용어 풀이. **저장소 밖 상위 폴더** |
| [`server/docs/README.md`](server/docs/README.md) | 서버 진입점 + 모듈 구조 + MQTT 토픽 전체 |
| [`server/docs/DISPATCH_FLOW.md`](server/docs/DISPATCH_FLOW.md) | 주문→cmd 발행 디스패치 흐름 (한글 cheat sheet) |
| [`server/docs/REFACTOR_F.md`](server/docs/REFACTOR_F.md) | 경로/예약 재설계(REFACTOR F) 내역 — 완료된 작업 기록 |
| [`isaac_simulation/README.md`](isaac_simulation/README.md) | Isaac Sim 5.1.0 시뮬레이션 + 디지털 트윈 상세 |
| [`hardware/INTEGRATION.md`](hardware/INTEGRATION.md) | **실물 붙이는 날 따라가는 bring-up 절차서** |
| [`hardware/README.md`](hardware/README.md) | 실물 RPi 레퍼런스: bridge_rpi + RpiCamera + UART 프로토콜 |
| [`virtual_test/README.md`](virtual_test/README.md) | 실물 없이 돌리기 (pytest / SIL / 벤치 하네스 3모드) |
| `archive/webots_simulation_old/` | Webots (레거시, 아카이브) |
