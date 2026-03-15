# AGV 자동화 물류 피킹 시스템

STM32 + Raspberry Pi 기반 AGV 2대가 KIVA 스타일 이동식 선반을 작업대로 운반하는 자동화 물류 시스템.
중앙 서버가 경로 계획, 작업 스케줄링, 충돌 회피를 담당하고 MQTT로 AGV를 제어한다.
Isaac Sim 5.1.0으로 시뮬레이션 환경 구성 중.

---

## 목차

1. [시스템 구성](#1-시스템-구성)
2. [디렉토리 구조](#2-디렉토리-구조)
3. [맵 구조](#3-맵-구조)
4. [통신 프로토콜](#4-통신-프로토콜)
5. [서버 아키텍처](#5-서버-아키텍처)
6. [로봇 상태 머신](#6-로봇-상태-머신)
7. [핵심 알고리즘](#7-핵심-알고리즘)
8. [Isaac Sim 시뮬레이션](#8-isaac-sim-시뮬레이션)
9. [실행 방법](#9-실행-방법)
10. [하드웨어 구성](#10-하드웨어-구성)
11. [향후 계획](#11-향후-계획)

---

## 1. 시스템 구성

```
+------------------+     MQTT      +----------------+     MQTT      +------------------+
|  warehouse_gui   | ----------->  |  AGV 서버      | ----------->  |  AGV (Isaac Sim) |
|  (Kivy, RPi)     |  주문/완료    |  (Python)      |  /agv/plan   |  Isaac Sim 5.1.0 |
+------------------+               +----------------+  /agv/control +------------------+
                                          |              /agv/shelf_cmd      |
+------------------+     HTTP      +------+-------+                          |
|  warehouse_server| <-----------> |   SQLite DB  |  /agv/arrived           |
|  (Flask, 재고DB)  |              |   주문/재고   | <------------------------+
+------------------+               +--------------+  /agv/shelf_ack
                                                     /agv/marker
```

### 구성 요소

| 구성 요소 | 역할 | 기술 |
|-----------|------|------|
| AGV 서버 | 경로 계획, 작업 스케줄링, 충돌 회피 | Python, MQTT |
| Isaac Sim | AGV 및 창고 3D 시뮬레이션 | Isaac Sim 5.1.0, USD |
| warehouse_gui | 작업자 터치스크린 주문/완료 GUI | Kivy, MQTT, Raspberry Pi |
| warehouse_server | 재고 및 주문 관리 REST API | Flask, SQLite |
| AGV 실물 | 자율 이동 + 선반 리프트 | STM32, Raspberry Pi 5 |

---

## 2. 디렉토리 구조

```
TU_Capstone_Design/
|
+-- isaac_simulation/           # Isaac Sim 시뮬레이션 (현재 개발 중)
|   +-- hello_world.py          # Step 1: 씬 레이아웃 확인
|   +-- step2_environment.py    # Step 2: 선반, 작업대, ArUco 마커
|   +-- step3_agv_mqtt.py       # Step 3: AGV 이동 + MQTT 연동
|   +-- step4_lift_shelf.py     # Step 4: 리프트 + 선반 이동 (USD API)
|   +-- step5_camera_aruco.py   # Step 5: 가상 카메라 ArUco 감지 (개발 중)
|   +-- README.md               # 이 파일
|
+-- webots_simulation/
|   +-- server/                 # AGV 서버 (시뮬레이터와 무관, MQTT 기반)
|   |   +-- main.py             # 서버 진입점
|   |   +-- config.py           # 설정값 (포트, 파일경로, MQTT 토픽)
|   |   +-- request_handler.py  # 핵심: 요청 처리 + 로봇 배정 알고리즘
|   |   +-- task_manager.py     # 작업 분해 (서브태스크 시퀀스 생성)
|   |   +-- task_scheduler.py   # Nearest Neighbor 선반 방문 순서 최적화
|   |   +-- robot_manager.py    # 로봇 6단계 상태 머신
|   |   +-- shelf_manager.py    # 선반 상태 추적
|   |   +-- staging_manager.py  # 작업대 회랑 진입/퇴출 게이팅
|   |   +-- path_planner.py     # A* 시간 기반 경로 계획
|   |   +-- mqtt_publisher.py   # MQTT 발행
|   |   +-- db_loader.py        # 엑셀 주문 DB 로더
|   |
|   +-- config/                 # 맵, 선반, 로봇 설정 JSON (서버/시뮬 공용)
|   |   +-- map.json            # 8x4 그리드 맵 (34노드)
|   |   +-- shelf_config.json   # 선반/물품/작업대 설정
|   |   +-- robot_config.json   # AGV 홈 노드 등
|   |
|   +-- Database/               # 주문 엑셀 데이터 (db_loader가 읽음)
|   +-- mqtt_test.py            # CLI 테스트 도구
|   +-- FLOWCHART.md            # 알고리즘 플로우차트 + 수정 이력
|
+-- warehouse_gui_server/       # 작업자 GUI + 재고 서버
|   +-- warehouse_gui.py        # Kivy 터치스크린 GUI (Raspberry Pi 실행)
|   +-- warehouse_server.py     # Flask HTTP 서버 + SQLite 재고 관리
|   +-- excel_to_sqlite.py      # 엑셀 -> SQLite DB 변환 (최초 1회)
|   +-- admin_gui.py            # 관리자 GUI
|
+-- archive/                    # 이전 버전 (v1~v3, 참조용)
+-- README.md
```

---

## 3. 맵 구조

```
W1(33)-- 1 - 2 - 3 - 4 - 5 - 6 - 7 - 8    (row 0, 통로)
          |   |   |   |   |   |   |   |
          9 -10 -[11]-[12]-13-[14]-[15]-16   (row 1, []=선반)
          |   |   |   |   |   |   |   |
         17 -18 -[19]-[20]-21-[22]-[23]-24   (row 2, []=선반)
          |   |   |   |   |   |   |   |
W2(34)-- 25 -26 -27 -28 -29 -30 -31 -32    (row 3, 통로)
```

| 항목 | 값 |
|------|----|
| 전체 노드 | 34개 (8x4 그리드 + 작업대 2개) |
| 선반 노드 | 8개 — 11, 12, 14, 15, 19, 20, 22, 23 |
| 작업대 | W1=33, W2=34 |
| W1 회랑 | gateway=1, staging=9, trigger=2 |
| W2 회랑 | gateway=25, staging=17, trigger=26 |
| 노드 타입 | M(통로), S(선반), W(작업대) |

---

## 4. 통신 프로토콜

### MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server -> AGV | 경로 계획 (노드 경로 + 타임스텝) |
| `/agv/shelf_cmd` | Server -> AGV | 선반 리프트 명령 (pickup / putdown) |
| `/agv/control` | Server -> AGV | resume 명령 (NODE_WAIT 해제) |
| `/agv/arrived` | AGV -> Server | 목표 노드 도착 / 중간 노드 위치 보고 |
| `/agv/shelf_ack` | AGV -> Server | 리프트 동작 완료 알림 |
| `/agv/marker` | AGV -> Server | ArUco 마커 감지 (트리거 노드 통과) |
| `agv/algorithm` | GUI/CLI -> Server | 주문/완료 명령 수신 |
| `warehouse/agv/at_ws` | Server -> GUI | AGV 작업대 도착 알림 (선반 셀 활성화) |

### 메시지 형식

경로 발행:
```json
{
  "robots": [
    {
      "rid": 1,
      "start": 33,
      "goal": 11,
      "node_path": [33, 1, 9, 10, 11],
      "timed_path": [{"node": 33, "t": 0}, {"node": 1, "t": 1}, ...]
    }
  ]
}
```

선반 리프트 명령:
```json
{"rid": 1, "command": "pickup", "shelf_id": 11}
```

주문 시작:
```json
{"type": "start_order", "사용자ID": 1, "주문번호": 1}
```

선반 피킹 완료:
```json
{"type": "shelf_complete", "사용자ID": 1}
```

### CLI 테스트 도구 명령 (mqtt_test.py)

| 명령 | 동작 |
|------|------|
| `시작 1` / `시작 2` | 사용자 주문 시작 |
| `선반완료 1 [노드번호]` | 특정 선반 피킹 완료 |
| `완료 1` / `완료 2` | 주문 전체 완료 |

---

## 5. 서버 아키텍처

서버는 MQTT 기반으로 시뮬레이터와 독립적으로 동작한다.
Isaac Sim, 실물 하드웨어 어느 쪽이든 MQTT 토픽만 맞으면 교체 가능.

### 모듈별 역할

```
main.py
  +-- MQTT 브로커 구독 (arrived, shelf_ack, marker, algorithm)
  +-- WebSocket 서버 시작 (Admin UI)

request_handler.py  [핵심]
  +-- _handle_start_order()      : 주문 시작 -> DB 로드 -> 선반 배정 -> 경로 발행
  +-- _handle_shelf_complete()   : 선반 피킹 완료 -> 반납 or 포워딩 결정
  +-- _handle_robot_arrived()    : 최종 도착 -> 다음 서브태스크 실행
  +-- _handle_robot_position()   : 중간 위치 갱신 -> resume 판단 -> 회랑 해제
  +-- _handle_pickup_ack()       : 리프트 완료 -> 작업대로 이동 명령
  +-- _handle_putdown_ack()      : 내려놓기 완료 -> 다음 선반 or IDLE
  +-- _handle_mqtt_marker()      : ArUco 마커 -> 회랑 해제
  +-- _plan_and_publish_move()   : STG 체크 포함 경로 계획 + MQTT 발행
  +-- _try_assign_pending_tasks(): PENDING 작업 재배정 루프

task_manager.py
  +-- 서브태스크 시퀀스 생성 (GO_TO_SHELF -> PICKUP -> DELIVER -> WAIT -> RETURN)
  +-- handle_shelf_complete()    : 선반 단위 완료 처리
  +-- rotate_shelf_to_end()      : 블록 선반을 순서 뒤로

task_scheduler.py
  +-- Nearest Neighbor 알고리즘으로 선반 방문 순서 최적화

robot_manager.py
  +-- 6단계 상태 머신 관리
  +-- get_available_robot()      : 유휴 AGV 탐색

shelf_manager.py
  +-- IN_PLACE / CARRIED / AT_WORKSTATION 상태 추적
  +-- 선반별 담당 AGV 기록

staging_manager.py
  +-- should_stage()             : 회랑 진입 가능 여부 판단
  +-- handle_marker_trigger()    : ArUco 트리거 -> 대기 AGV 해제
  +-- check_position_release()   : 위치 기반 회랑 자동 해제
  +-- release_corridor_without_trigger(): 포워딩 시 즉시 해제

path_planner.py
  +-- astar_with_time()          : 시간 기반 A* (예약 기반 충돌 회피)
  +-- 선반 노드 통과 제외 (출발/도착만 허용)
```

### 서브태스크 타입

```
GO_TO_SHELF -> PICKUP_SHELF -> DELIVER_TO_WS -> WAIT_PICKING -> RETURN_SHELF
                                                             \-> FORWARD_SHELF (포워딩 시)
```

---

## 6. 로봇 상태 머신

```
IDLE -> MOVING_TO_SHELF -> PICKING_UP_SHELF -> DELIVERING_TO_WS -> WAITING_FOR_PICK
                                                                          |
                                           +------------------------------+
                                           |                              |
                                    [다른 WS 필요]                  [불필요]
                                    FORWARD_SHELF               RETURNING_SHELF
                                           |                              |
                                    WAITING_FOR_PICK             [다음 선반?]
                                                              Yes -> MOVING_TO_SHELF
                                                              No  -> IDLE
```

### 선반 상태 전환

| 이벤트 | 호출 | 결과 상태 |
|--------|------|----------|
| AGV가 작업대 도착 | `mark_shelf_at_workstation()` | AT_WORKSTATION |
| shelf_complete 수신 (선반 출발) | `mark_shelf_picked_up()` | CARRIED |
| 홈 putdown 완료 | `mark_shelf_returned()` | IN_PLACE |

---

## 7. 핵심 알고리즘

### STG — 작업대 회랑 게이팅

작업대 회랑(작업대 + gateway 노드)은 한 번에 1대만 점유 가능.

```
AGV가 WS로 이동 요청
  |
  +-> should_stage(ws_node, rid)
        |
        +-> 회랑 비어있음 -> 진입 허가 -> 회랑 점유 기록
        |
        +-> 회랑 점유 중 -> staging 노드로 우회 경로 발행 -> 대기 큐 등록
```

회랑 해제 방식:
- 주: 위치 기반 — is_exiting 로봇이 회랑 구역(ws, gateway) 밖으로 나가면 자동 해제
- 보조: ArUco 마커 — trigger 노드 통과 시 `handle_marker_trigger()` 호출
- 포워딩: `release_corridor_without_trigger()` — 소스 회랑 즉시 해제

### TRG — ArUco 마커 트리거

```
AGV가 trigger 노드(2 or 26) 통과
  -> /agv/marker 발행
  -> handle_marker_trigger() 호출
  -> 대기 큐의 다음 AGV 해제 -> 새 경로 발행
```

### Node U — 복귀 중 인터셉트

```
선반A가 WS1에서 완료 -> RETURNING_SHELF (선반 홈으로 복귀 중)
  |
새 주문에서 선반A가 WS2에 필요
  |
_try_intercept_returning_shelf() 호출
  -> 기존 WS1 회랑 즉시 해제
  -> 복귀 경로를 WS2로 재계획
  -> FORWARD_SHELF 서브태스크 삽입
```

### NODE_WAIT — 서버 기반 노드 단위 이동

```
AGV 중간 노드 도착
  -> /agv/arrived (type: robot_position) 발행
  -> 서버 _is_safe_to_resume() 확인
        |
        +-> 안전 (다음 노드에 다른 AGV 없음) -> /agv/control resume 발행
        |
        +-> 위험 (충돌 예상)                -> 대기 (다음 도착 시 재확인)
```

### F 노드 — 선반 가용성 6분기

| 선반 상태 | 조건 | 결정 |
|-----------|------|------|
| IN_PLACE | 다른 AGV 미배정 | go — 선반 홈으로 이동 |
| IN_PLACE | 다른 AGV GO_TO_SHELF 중 | pending — 순서 회전 시도 |
| CARRIED | 이동 중 | pending — 순서 회전 시도 |
| AT_WORKSTATION | carrier WAITING_FOR_PICK 중 | pending |
| AT_WORKSTATION | WS 회랑 점유 중 | pending |
| AT_WORKSTATION | 진입 가능 | direct — WS로 직행 |

### 다중 로봇 공정 배정

```
주문 1개 -> 선반 N개 -> 태스크 N개로 분리 (task_id: T{user}_{order}_{idx})
  |
_count_active_robots_per_ws() : 각 WS별 현재 활성 로봇 수 계산
  |
get_next_pending_task_fair()  : 활성 로봇이 적은 WS 태스크 우선 배정
  |
블록 태스크 -> blocked_task_ids에 추가 후 건너뜀 (교착 방지)
```

---

## 8. Isaac Sim 시뮬레이션

### 진행 상태

| 단계 | 내용 | 상태 |
|------|------|------|
| Step 1 | 8x4 창고 씬 레이아웃 확인 | 완료 |
| Step 2 | ArUco 바닥 마커 + 3층 선반 + 작업대 | 완료 |
| Step 3 | AGV 이동 + MQTT 연동 | 완료 |
| Step 4 | 리프트 애니메이션 + 선반 이동 (USD API) | 코드 완료, 런타임 검증 필요 |
| Step 5 | 가상 카메라 ArUco 감지 (근접 기반) | 개발 중 |

### 실행 방법

```bash
# 절대 경로 필수
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step5_camera_aruco.py
```

### AGV 모델 구조

```
AGV Body (VisualCuboid, 0.3 x 0.2 x 0.08m)
  +-- 바퀴 좌 (VisualCylinder)
  +-- 바퀴 우 (VisualCylinder)
  +-- 시저리프트
       +-- 막대 A (X자 좌)
       +-- 막대 B (X자 우)
       +-- 상판 (VisualCuboid, z=0.25 -- 선반 1층 z=0.40 아래로 진입 가능)
```

### 선반 모델 구조

```
Shelf_{node_id} (Xform, delta 이동용 루트)
  +-- 다리 x4 (VisualCylinder, h=0.85m)
  +-- 선반판 1층 (z=0.40)
  +-- 선반판 2층 (z=0.62)
  +-- 선반판 3층 (z=0.84)
```

### Step 4 — 선반 delta 이동 방식

`world.reset()` 호출 시 루트 prim의 translate가 초기화되는 Isaac Sim 5.1.0 버그를 우회하기 위해
루트 Xform에 translate를 직접 설정하지 않고, 이동 시에만 원래 위치 대비 delta를 적용한다.

```python
# 선반 생성 시: 루트에 translate 없음, 자식은 절대 좌표
shelf_origins[node_id] = (x, y)

# AGV가 선반 운반 중:
orig = shelf_origins[carrying_shelf]
dx, dy = agv.pos[0] - orig[0], agv.pos[1] - orig[1]
xform.AddTranslateOp().Set(Gf.Vec3d(dx, dy, 0.0))
```

### Step 5 — 가상 카메라 ArUco 감지

IsaacCamera render product 방식은 Isaac Sim 5.1.0 스크립팅 모드에서 omni.syntheticdata.plugin
충돌로 안정적으로 동작하지 않는다. 대신 근접 기반 가상 감지를 사용한다.

```python
CAM_DETECT_RADIUS = 0.087  # m (카메라 높이 0.15m, 화각 30도 기준)
DETECT_INTERVAL   = 5      # 프레임마다 감지

# 매 DETECT_INTERVAL 프레임:
# AGV 위치에서 반경 CAM_DETECT_RADIUS 이내 노드 마커를 감지한 것으로 처리
# 직전에 감지한 마커와 동일하면 중복 발행 방지
```

### USD 핵심 패턴 (Isaac Sim 5.1.0)

```python
# prim 위치 업데이트
prim.GetAttribute("xformOp:translate").Set(Gf.Vec3d(x, y, z))

# 머티리얼 연결
input.ConnectToSource(shader.ConnectableAPI(), "출력이름", UsdShade.AttributeType.Output)
material.CreateSurfaceOutput().ConnectToSource(
    shader.ConnectableAPI(), "surface", UsdShade.AttributeType.Output
)

# 텍스처 파일 경로
tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(png_path))
```

### MQTT 스레드 안전성 (step5 핵심 패턴)

MQTT 콜백은 별도 스레드에서 실행되므로, AGV 상태를 직접 수정하면 race condition 발생.
pending 필드를 통해 main loop에서만 상태를 변경한다.

```python
# MQTT 스레드: pending에 저장만
agv._pending_plan = (node_path, goal)

# main loop: pending 소비 후 상태 변경
if agv._pending_plan is not None:
    node_path, goal = agv._pending_plan
    agv._pending_plan = None
    if agv.state == "IDLE":
        agv.pos = node_xy(start_node).copy()
        agv.set_plan(...)
    elif agv.state == "NODE_WAIT" and agv.current_node == start_node:
        # 이미 해당 노드 대기 중 -> arrived 재발행해서 서버 resume 트리거
        agv.path_queue = list(node_path[1:])
        bridge._publish_arrived(agv.rid, start_node)
    else:
        # MOVING 중 -> path만 교체
        agv.path_queue = list(node_path[1:])
```

---

## 9. 실행 방법

### 사전 요구사항

```bash
# MQTT 브로커
sudo apt install mosquitto mosquitto-clients
sudo systemctl start mosquitto

# 외부 접속 허용 (GUI가 다른 기기에 있을 경우)
# /etc/mosquitto/conf.d/external.conf 생성:
# listener 1883
# allow_anonymous true
sudo systemctl restart mosquitto

# 서버 의존성
pip install paho-mqtt websockets pandas openpyxl

# Isaac Sim 의존성 (Isaac Sim Python 환경 내)
# paho-mqtt 별도 설치 필요
```

### 시뮬레이션 실행 (터미널 3개)

```bash
# 터미널 1: AGV 서버
cd TU_Capstone_Design/webots_simulation
python3 -m server.main

# 터미널 2: Isaac Sim 시뮬레이션
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step5_camera_aruco.py

# 터미널 3: CLI 테스트
cd TU_Capstone_Design/webots_simulation
python3 mqtt_test.py
```

### GUI 포함 전체 실행 (네트워크 필요)

```bash
# 노트북 터미널 1: AGV 서버
cd TU_Capstone_Design/webots_simulation && python3 -m server.main

# 노트북 터미널 2: 재고 서버 (최초 1회 DB 생성 필요)
cd TU_Capstone_Design/warehouse_gui_server
python3 excel_to_sqlite.py   # 최초 1회만
python3 warehouse_server.py

# 노트북 터미널 3: Isaac Sim
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step5_camera_aruco.py

# Raspberry Pi: GUI
cd TU_Capstone_Design/warehouse_gui_server
python3 warehouse_gui.py
# -> 시작 시 노트북 핫스팟 IP 입력 팝업 (hostname -I 로 확인)
```

네트워크: 휴대폰 핫스팟에 노트북, Raspberry Pi, AGV 실물 모두 연결.

---

## 10. 하드웨어 구성

### AGV 하드웨어 스택

```
+---------------------------+
|      Raspberry Pi 5       |  상위 제어부
|  - MQTT 수신 (/agv/plan)  |
|  - 카메라: ArUco 감지     |
|  - UART -> STM32 명령     |
+---------------------------+
           | UART
+---------------------------+
|          STM32            |  하위 제어부
|  - 모터 드라이버 (바퀴)   |
|  - 리니어 액추에이터 (리프트) |
|  - 엔코더 피드백          |
+---------------------------+
```

### AGV 실물 통신 흐름

```
서버 --MQTT--> RPi --UART--> STM32 --PWM--> 모터/액추에이터
서버 <-MQTT-- RPi <-UART-- STM32          (arrived, shelf_ack)
카메라 -> RPi (OpenCV ArUco) --MQTT--> 서버 (marker)
```

---

## 11. 향후 계획

### 단기 (시뮬레이션 완성)

| 항목 | 내용 |
|------|------|
| Step 4 런타임 검증 | delta 방식 선반 이동 실제 실행 확인 |
| Step 5 완성 | 가상 ArUco 감지 + 회랑 해제 전체 흐름 검증 |
| GUI 버그 수정 | shelf_complete 시 선반 ID 검증 누락 (어떤 선반 셀 눌러도 완료 처리되는 문제) |
| 다중 로봇 공정 배정 검증 | GUI 포함 2대 동시 운영 시뮬레이션 확인 |

### 중기 (하드웨어 연동)

| 항목 | 내용 |
|------|------|
| RPi MQTT 브릿지 | 서버 <-> RPi MQTT 연동 코드 작성 |
| STM32 UART 프로토콜 | RPi <-> STM32 명령 포맷 정의 |
| 카메라 연동 | RPi 카메라 모듈 + OpenCV ArUco 실물 테스트 |
| 리프트 캘리브레이션 | 선반 높이 대비 리프트 스트로크 튜닝 |

### 장기 (선택적)

| 항목 | 내용 |
|------|------|
| AGV 내부 ROS2 | RPi 내부에서 MQTT Bridge node + Camera node + micro-ROS (STM32) 구성. 서버는 MQTT 유지 |
| Admin 웹 대시보드 | 실시간 맵 뷰 + 로봇 상태 모니터링 |
| 타임아웃/에러 복구 | AGV 응답 없음 시 재시도 + 경보 로직 |
| 동적 맵 확장 | 선반/작업대 수 설정 파일로 동적 변경 |

---

## 의존성

### 서버

```
paho-mqtt >= 1.6.0
websockets >= 10.0
pandas
openpyxl
```

### warehouse_gui (Raspberry Pi)

```
kivy
paho-mqtt
requests
```

### warehouse_server

```
flask
pandas
openpyxl
```

### Isaac Sim 환경

Isaac Sim 5.1.0 내장 Python 사용. 추가 설치:
```bash
~/isaacsim/_build/linux-x86_64/release/python.sh -m pip install paho-mqtt
```
