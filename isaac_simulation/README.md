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
|  (Kivy, RPi)     |  주문/완료    |  (Python)      |  /agv/cmd    |  Isaac Sim 5.1.0 |
+------------------+               +----------------+               +------------------+
                                          |                                  |
+------------------+     HTTP      +------+-------+    /agv/marker           |
|  warehouse_server| <-----------> |   SQLite DB  | <------------------------+
|  (Flask, 재고DB)  |              |   주문/재고   |  /agv/cmd_ack
+------------------+               +--------------+
```

**cmd-based 통신 개요:**
- 서버 → AGV: `/agv/cmd` 에 단일 명령 (forward/turn_left/turn_right/turn_180/lift_up/lift_down)
- AGV → 서버: `/agv/marker` (마커 감지 + heading), `/agv/cmd_ack` (회전/리프트 완료)

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
+-- isaac_simulation/           # Isaac Sim (현재 메인) — Isaac 전용
|   +-- hello_world.py ~ step5_camera_aruco.py   # Step 1~5 (완료)
|   +-- step7_kinematic.py      # ★ 현재 메인: Kinematic Physics + 디지털 트윈(TWIN=1)
|   +-- step6_visual.py         # Step 6: 시각 전용 (물리 없음, 트윈 미지원)
|   +-- run_twin.sh             # 트윈 모드 실행 래퍼 (TWIN=1 + 칸당 소요시간)
|   +-- bridge_isaac.py         # Isaac 전용 bridge (콜백)
|   +-- isaac_hw.py             # IsaacMotors (가상)
|   +-- camera.py               # IsaacCamera (proximity 가상 마커 감지)
|   +-- capture_agv.py          # 발표자료용 정적 포즈 촬영 (MQTT/서버 없음)
|   +-- Presentation_manual.py  # 발표용 수동 제어 데모 (터미널로 노드 지정)
|   +-- STEP8_PLAN.md / README.md
|
+-- hardware/                   # AGV 실물(RPi) 전용 (주원이 STM 펌웨어 대응)
|   +-- INTEGRATION.md          # 실물 붙이는 날 bring-up 절차서
|   +-- rpi_main.py             # 실물 진입점 (camera+bridge, AGV_ID로 rid)
|   +-- bridge_rpi.py           # MQTT ↔ UART 브릿지
|   +-- camera.py               # RpiCamera (주원이 opencv ArUco 비전)
|   +-- camera_preview.py       # 초점 맞추기용 웹 프리뷰
|   +-- config.py               # 배포 설정 (서버IP/UART/카메라/HEADING_OFFSET)
|   +-- stm32/rpi_uart.c, AGV_Control.zip   # 주원이 STM 소스/펌웨어
|
+-- server/                     # AGV 서버 (MQTT 기반) — 2026-06-13 레이어 분리
|   +-- main.py  config.py
|   +-- comm/ core/ planning/ managers/ data/ docs/   # 상세는 루트 README / CLAUDE.md
|
+-- virtual_test/               # 실물 없이 돌리기 (algorithm=pytest / bench_camera=벤치 / SIL)
|
+-- FLOWCHART.md                # 알고리즘 플로우차트 + 수정 이력
|
+-- warehouse_gui_server/       # 작업자 GUI + 재고 서버
|   +-- warehouse_gui_ws1.py    # 1번 라파(작업대1) GUI / ws2.py = 2번 라파
|   |                           #   [주의] warehouse_gui_v2.py는 ws2 사본 — 1번에서 열지 말 것
|   +-- warehouse_server_v2.py  # Flask HTTP + MQTT + SQLite 재고 관리
|   +-- excel_to_sqlite.py      # 데이터 베이스.xlsx -> warehouse.db (최초 1회)
|   +-- 사용자{1,2}주문.xlsx    # 주문 데이터
|   +-- 데이터 베이스.xlsx      # 재고 마스터
|
+-- archive/                    # 이전 버전 (v1~v3, webots, 구 mqtt_test.py — 참조용)
+-- README.md
```

---

## 3. 맵 구조

```
          0 - 1 - 2 - 3 - 4 - 5 - 6 - 7    (row 0, 통로)
          |   |   |   |   |   |   |   |
W2(8)---  8 - 9 -10 -11 -12 -13 -14 -15    (row 1, W2 작업대)
          |   |   |   |   |   |   |   |
         16 -17 -[18]-[19]-20-[21]-[22]-23   (row 2, []=선반)
          |   |   |   |   |   |   |   |
         24 -25 -[26]-[27]-28-[29]-[30]-31   (row 3, []=선반)
          |   |   |   |   |   |   |   |
W1(32)-- 32 -33 -34 -35 -36 -37 -38 -39    (row 4, W1 작업대)
          |   |   |   |   |   |   |   |
         40 -41 -42 -43 -44 -45 -46 -47    (row 5, 통로)

노드 = ArUco 마커 ID (항등). 0-based — 바닥 마커 0~47을 그 번호 칸에 붙인다.
```

| 항목 | 값 |
|------|----|
| 전체 노드 | 48개 (8×6 그리드, **0~47**) |
| 선반 노드 | 8개 — 18, 19, 21, 22, 26, 27, 29, 30 |
| 작업대 | W1=32 (user_id=1), W2=8 (user_id=2) |
| W1 회랑 | gateway=24, staging=40, trigger=33 |
| W2 회랑 | gateway=16, staging=0,  trigger=9 |
| AGV 홈 | AGV-1: node 8 (W2), AGV-2: node 32 (W1) |
| 노드 타입 | M(통로), S(선반), W(작업대) |

---

## 4. 통신 프로토콜

### MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/cmd` | Server → AGV | 이동/회전/리프트 명령 |
| `/agv/marker` | AGV → Server | ArUco 마커 감지 (위치 + heading) |
| `/agv/cmd_ack` | AGV → Server | 회전/리프트 완료 알림 |
| `/agv/presence` | AGV → Server | 접속/이탈 (retained + LWT, 수정 75) |
| `/agv/pose` | AGV → **트윈** | 연속 자세 스트림 (수정 68) — **서버는 구독 안 함** |
| `warehouse/order/start` `warehouse/shelf/complete` `warehouse/order/complete` | GUI → Server | 주문 시작/선반완료/주문종료 |
| `warehouse/shelf/arrived` | Server → GUI | AGV 작업대 도착 알림 (선반 셀 활성화) |

### 메시지 형식

서버 → AGV 명령 (`/agv/cmd`):
```json
{"rid": 1, "cmd": "forward", "target_node": 24, "timestamp": 1700000000.0}
{"rid": 1, "cmd": "turn_left"}
{"rid": 1, "cmd": "turn_180"}
{"rid": 1, "cmd": "lift_up", "shelf_id": 18}
{"rid": 1, "cmd": "lift_down", "shelf_id": 18}
```
- `target_node` (수정 70): forward의 **도착 예정 노드**. 없으면 AGV/트윈이 자기 heading으로
  "내 앞이 어디지?"를 스스로 계산해야 하고, heading이 한 번 어긋나면 **서버와 서로 다른 목적지로
  해석해 교착**한다. 서버는 이미 목적지를 아니까 그냥 알려준다. (실물은 무시해도 됨 — 그냥 앞으로 감)
- `shelf_id`: lift 대상 선반. 시뮬이 "근처 선반"을 추측하지 않게 한다.

AGV → 서버 위치 보고 (`/agv/marker`):
```json
{"rid": 1, "marker_id": 14, "heading": 90, "ts": 1700000000}
{"rid": 1, "marker_id": 14, "heading_observed": 87, "ts": 1700000000}
```
- `heading`: 0=North, 90=East, 180=South, 270=West. **서버가 제어에 그대로 쓴다** (시뮬이 보내는 신뢰된 값).
- `heading_observed` (수정 69): 실물 카메라가 계산한 값. **서버는 비교/로그만 하고 제어엔 안 쓴다.**
  `HEADING_OFFSET` 실측 후 `server/config.py`의 `TRUST_CAMERA_HEADING=True`로 밸브를 연다.

AGV → 서버 완료 알림 (`/agv/cmd_ack`):
```json
{"type": "cmd_ack", "rid": 1, "cmd": "turn_left", "status": "done"}
{"type": "cmd_ack", "rid": 1, "cmd": "lift_up",   "status": "done"}
```

AGV → 서버 접속/이탈 (`/agv/presence`, retain=True + LWT):
```json
{"rid": 1, "online": true}
{"rid": 1, "online": false}     // LWT — 브로커가 대신 발행 (전원/네트워크 끊김)
```
- 안 켠 로봇에는 태스크를 배정하지 않는다. 단, **끊긴 로봇의 몸은 그 칸에 남아 있으므로**
  A* 장애물로는 계속 취급한다 (`online` ≠ `ever_seen`).

주문 시작 (`warehouse/order/start`):
```json
{"type": "start_order", "사용자ID": 1, "주문번호": 1, "작업대": 2}
```
- `작업대`는 **user_id와 별개 개념** (수정 53). 사용자 1이 작업대 2에서 일할 수 있다.

선반 피킹 완료 (`warehouse/shelf/complete`) — 서버는 `작업대`로 어느 회랑을 비울지 찾는다:
```json
{"type": "shelf_complete", "사용자ID": 1, "작업대": 2}
```

서버 → GUI 선반 도착 (`warehouse/shelf/arrived`) — GUI가 해당 선반 셀만 활성화:
```json
{"작업대": 2, "사용자ID": 1, "선반번호": "1-1"}
```

---

## 5. 서버 아키텍처

서버는 MQTT 기반으로 시뮬레이터와 독립적으로 동작한다.
Isaac Sim, 실물 하드웨어 어느 쪽이든 MQTT 토픽만 맞으면 교체 가능.

### 모듈별 역할

> 2026-06-13 레이어 분리 — import는 패키지 경로(`from server.managers.robot import RobotManager`).
> 전체 구조는 [`../server/docs/README.md`](../server/docs/README.md)가 단일 진실. 여기는 요약.

```
main.py
  +-- MQTT 구독 배선 (/agv/marker, /agv/cmd_ack, /agv/presence, warehouse/order|shelf/*)
  +-- 상단 docstring = 이벤트↔핸들러↔다이어그램 매핑 지도

core/request_handler.py  [핵심]  — 3 mixin 다중상속
  core/_workflow_mixin.py   주문/태스크/F-노드/인터셉트
    +-- _handle_start_order()      : 주문 -> DB 로드 -> 태스크 N개 -> 배정
    +-- _handle_shelf_complete()   : 선반 피킹 완료 -> 반납 or 포워딩 결정 (Point C)
    +-- _get_shelf_availability()  : F-노드 선반 가용성 분기
    +-- _try_assign_pending_tasks(): PENDING 작업 재배정 루프
  core/_marker_mixin.py     AGV 이벤트 — 주행 엔진
    +-- _handle_marker_report()    : /agv/marker -> 위치 갱신 -> 다음 cmd
    +-- _handle_cmd_ack()          : /agv/cmd_ack -> turn/lift 완료
    +-- _handle_presence()         : /agv/presence -> 접속/이탈 (수정 75)
    +-- handle_marker_trigger()    : TRG -> 대기 AGV 해제
  core/_movement_mixin.py   ★ 이동 명령 발행 + 충돌/교착 회피
    +-- _plan_and_publish_move()   : STG 체크 포함 경로 계획 -> cmd 큐
    +-- _send_next_command()       : forward 충돌 체크 + 노드 락 획득
    +-- _try_dispatch_all()        : 막힌 로봇 재시도 + 교착 감지 (ACK 도착마다 호출)
    +-- _detect_deadlock_cycle()   : wait-for 사이클 감지 (수정 54)
    +-- _resolve_deadlock(cycle)   : 사이클 중 한쪽을 우회 재계획

planning/reservation_service.py   미래 점유 단일 진실 (시공간 예약) — 충돌/교착 예방의 핵심
planning/path_planner.py          astar_with_time() — 예약 연동 A*, turn_penalty=0.3
planning/deadlock_detector.py     wait-for 사이클 감지 (순수 함수, 수정 54)
planning/command_queue.py         AGV별 cmd lifecycle (in_flight 단일 슬롯)
planning/order_optimizer.py       Nearest Neighbor 선반 방문 순서 최적화

managers/robot.py     6단계 상태 머신 + get_available_robot() + presence
managers/shelf.py     IN_PLACE / CARRIED / AT_WORKSTATION 추적
managers/staging.py   should_stage() / handle_marker_trigger() / release_corridor_without_trigger()
managers/task.py      서브태스크 시퀀스 (GO_TO_SHELF -> PICKUP -> DELIVER -> WAIT -> RETURN)
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
AGV가 trigger 노드(W1=34 or W2=10) 통과
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

### 충돌 회피 — cmd-based 노드 예약

> **노드 락(통행권) 모델** (수정 55). 예전엔 서버가 각자 `_reserved_nodes` set을 들고 있었지만,
> 지금은 **`ReservationService`가 미래 점유의 단일 진실**이다. "누가 언제 어디 있을 것인가"를
> 한 곳에서만 관리하니, 예약과 실제 진행이 어긋나는 종류의 버그가 구조적으로 줄었다.
> 배경은 [`../../설계_근본해결_노트.md`](../../설계_근본해결_노트.md) (저장소 밖 상위 폴더 `Projects/`).

```
AGV가 forward 직전:
  _send_next_command() 충돌 체크
    +-> next_node를 남이 잡고 있음 -> 보류 (blocked)
    +-> 안전 -> 노드 락 획득 + forward 발행 (target_node 명시 — 수정 70)

AGV가 마커 도착:
  /agv/marker -> 위치 갱신 + 지나온 락 해제
  _try_dispatch_all() -> 보류 중인 다른 AGV 재시도 (ACK 도착마다)
    +-> 서로 맞물려 못 움직이면 -> _detect_deadlock_cycle()
                                   -> deadlock_detector.find_wait_cycle() (수정 54)
    +-> _resolve_deadlock(cycle) : 사이클 중 한쪽을 우회 재계획
```

추가 방어막:
- **마커 인접성 검사** (수정 62) — 지금 칸에서 **갈 수 없는 칸**의 마커 보고는 거부. 오검출로 인한 순간이동 차단
- **forward 목표 일치 검사** (수정 64) — 보낸 `target_node`와 다른 마커가 오면 거부
- **죽은 예약 청소** (수정 74) — 주인이 사라진 예약이 A*를 영영 막던 문제

> 구 NODE_WAIT 방식(`/agv/arrived` + `/agv/control` resume)은 cmd-based로 통합됨.

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
| Step 1 | 8x6 창고 씬 레이아웃 확인 | 완료 |
| Step 2 | ArUco 바닥 마커 + 3층 선반 + 작업대 | 완료 |
| Step 3 | AGV 이동 + MQTT 연동 | 완료 |
| Step 4 | 리프트 애니메이션 + 선반 이동 (USD API) | 완료 |
| Step 5 | 가상 카메라 ArUco 감지 (근접 기반, cmd-based) | 완료 |
| Step 6 | 바퀴 회전 + 터치스크린 + 선반 orient 유지 (`step6_visual.py`) | 완료 |
| **Step 7** | **Kinematic Physics + 디지털 트윈 (`step7_kinematic.py`) — 현재 메인** | 완료 (시뮬 검증 06-30, 실물 트윈 연동 07-11) |
| Step 8 | Articulation — 바퀴 JointDrive 물리 이동 | 미작성 (졸업 후, [`STEP8_PLAN.md`](STEP8_PLAN.md)) |

### 실행 방법 — 두 가지 역할을 구분할 것

Isaac은 **AGV 본인**이 될 수도, **디지털 트윈**(실물을 따라 그리는 화면)이 될 수도 있다.

```bash
# (A) AGV 역할 — 시뮬 단독 실행. Isaac이 직접 마커를 발행한다
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py

# (B) 트윈 역할 — 실물/벤치가 발행한 마커를 따라 그린다
./isaac_simulation/run_twin.sh          # = TWIN=1, 1칸 추정 3.0초
./isaac_simulation/run_twin.sh 5.0      # 실물이 느릴 때 칸당 5초로
```

> ⚠️ **TWIN=1을 빠뜨리고 실물과 동시에 켜면** Isaac이 트윈이 아니라 AGV 본인이 되어
> **마커를 직접 발행한다.** 발행자가 둘이 되어 서버가 도착을 두 번 받고 상태가 꼬인다.
>
> `step6_visual.py`는 트윈을 지원하지 않는다 (물리·TWIN 없는 시각 전용 버전).

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
Shelf_{node_id} (Xform 루트 — translate/orient 로 이동/회전)
  +-- 다리 x4 (VisualCuboid, h=0.85m, 루트 기준 로컬 좌표)
  +-- 선반판 1층 (z=0.40, 로컬 좌표)
  +-- 선반판 2층 (z=0.62, 로컬 좌표)
  +-- 선반판 3층 (z=0.84, 로컬 좌표)
  +-- 가로 빔 x3층x4방향 (로컬 좌표)
```

### Step 4 — 선반 delta 이동 방식 (구버전, Step 6 에서 변경됨)

> Step 6 에서 선반 회전 지원을 위해 아래 방식으로 교체됨.

이전 방식 (delta):
- 자식 prim을 절대 좌표로 생성 → 루트 translate = delta(dx, dy)
- 문제: 루트 orient 추가 시 자식이 world 원점 기준으로 회전해 위치가 틀어짐

### Step 6 — 선반 절대좌표 + orient offset 방식

`world.reset()` 이후에 루트 translate를 설정하면 reset 으로 초기화되지 않는다.
자식 prim 을 루트 기준 **로컬 좌표**로 생성해야 orient 회전이 올바르게 동작한다.

선반 orient를 pickup 시 고정(offset 저장)하여 운반/내려놓기 중에도 회전이 유지된다.

```python
# build_shelf(): 자식을 로컬 좌표로 생성 (x, y 오프셋 제거)
VisualCuboid(position=np.array([dx * LEG_OFFSET, dy * LEG_OFFSET, LEG_HEIGHT / 2]))

# world.reset() 이후: 루트에 translate 추가
UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(node_x, node_y, 0.0))

# execute_cmd("lift_up"): pickup 시 orient offset 저장
q_shelf = self._read_shelf_orient(shelf_id)        # 현재 선반 orient
q_agv   = self._heading_quat(self.heading)          # AGV heading → quatf
q_inv   = Gf.Quatf(q_agv.GetReal(), -q_agv.GetImaginary())
self.shelf_offset = q_inv * q_shelf                 # heading 대비 offset

# _sync_shelf(): 운반 중 — 절대 위치 + offset 유지
orient = self._heading_quat(self.heading) * self.shelf_offset
xform.translate = (agv.pos[0], agv.pos[1], lift_dz)
xform.orient    = orient   # heading 바뀌어도 선반 방향 고정

# _place_shelf(): putdown 완료 — 원위치, orient는 그대로 유지
xform.translate = (orig_x, orig_y, 0.0)
# ⚠️ orient 리셋하지 않음 — 선반 내 물품 배치 방향 유지
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

### Step 6 — 수정 이력

#### 2026-03-16 시각 버그 수정

| 버그 | 원인 | 수정 |
|------|------|------|
| AGV 앞 검정 센서 범프 | 장식용 VisualCuboid를 카메라로 혼동 | build_agv() 에서 제거 (카메라는 내부에 있음) |
| AGV 회전 시 바퀴 방향 고정 (오토바이 효과) | `_wheel_quat(angle)` 에 heading 미반영 | `q_head * q_roll * q_base` 방식으로 변경 |
| 시저리프트 레그가 상/하판 미연결 | arm_half=0.08 (너무 짧음), 비례식 공식 부정확 | arm_half=0.14 (scale 0.28), arccos 정확 기하학 공식 |
| AGV 회전 시 선반 미회전 | 자식 prim 절대 좌표, 루트 orient 없음 | 로컬 좌표로 변경 + orient op (heading) 추가 |
| GfQuatf vs GfQuatd 타입 오류 | Isaac Sim 이 orient op를 Quatf 로 생성 | `_set_quat_op()` — Quatd 시도 후 실패 시 Quatf 재시도 |

#### 2026-03-16 창고 시각화 추가 (발표용 현실감 개선)

| 항목 | 내용 |
|------|------|
| 선반 트레이 | 각 층마다 금속 트레이(바닥 + 4면 벽, 58×58cm) 추가 |
| 트레이 내 소품 | 층/선반별 결정론적 랜덤 4~6개 소품 (크기·색상 다양, `np.random.RandomState(node_id*31+lvl*7)`) |
| 작업대 → 컨베이어 | Y방향 롤러 컨베이어(롤러 8개) + 4다리 + 끝에 수납 박스 |
| 작업자 | 각 작업대 옆 1명 — 하체(파란 바지) + 상체(노란 작업복) + VisualSphere 머리 + 노란 안전모 |
| 창고 벽 | 3면 벽(왼/오른/뒤, 높이 4m), 카메라 방향 앞면 제거 |
| 천장 조명 | `UsdLux.SphereLight` 6개 (intensity=6000) + 흰 조명 기구 실린더 |

**배치 구조 (x축 단면):**

```
[왼쪽벽 x=-2.6]
  x≈-2.05  수납 박스 (컨베이어 +Y 끝)
  x=-1.55  컨베이어 중심 (Y방향, 롤러 8개)
  x=-1.05  작업자 (선반↔컨베이어 사이, 양팔 닿음)
  x≈-0.85  선반 왼쪽 끝 (LEG_OFFSET=0.35)
  x=-0.50  선반 중심 / WS 노드 (AGV 도착)
  x=+0.50  AGV 주행 통로
```

**트레이 치수:**

```python
TRAY_W  = 0.58   # 가로
TRAY_D  = 0.58   # 세로
TRAY_BH = 0.012  # 바닥 두께
TRAY_WH = 0.065  # 벽 높이   (박스 최대 높이 0.16보다 낮아 내용물 보임)
TRAY_WT = 0.025  # 벽 두께
# 내부 공간: 0.53×0.53 → 소품 최대 크기 0.13 충분히 수용
```

#### 시저리프트 기하학 (정확 공식)

```
body_top = BODY_Z(0.10) + scale_z_half(0.04) = 0.14
lift_bot = lift_z - plate_half(0.01)
arm_half = scale_z(0.28) / 2 = 0.14

center_z = (body_top + lift_bot) / 2
tilt     = arccos(half_h / arm_half)    # half_h = (lift_bot - body_top) / 2

lift_z=0.25 (하강): tilt ≈ 69°    lift_z=0.42 (상승): tilt ≈ 15°
```

#### GfQuatf/Quatd 타입 대응 패턴

Isaac Sim 은 `AddOrientOp()` 호출 시 내부적으로 `GfQuatf` (float) 로 저장하는 경우가 있음.
`GfQuatd` (double)를 set 하려 하면 타입 불일치 오류 발생.

```python
@staticmethod
def _set_quat_op(op, w, x, y, z):
    try:
        op.Set(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
    except Exception:
        op.Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)))

# 새 orient op 추가 시: PrecisionFloat 명시
xform.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)))
```

---

### Step 6 — cmd-based AGV 제어 + 시각 개선

#### AGV 이동 방식 (cmd-based)

서버가 경로를 한 칸씩 분해해서 명령을 발행함. AGV는 명령 하나씩 실행:

```
IDLE
  ├─ forward  → 현재 heading 방향으로 직선 이동 → 마커 감지 시 완료
  ├─ turn_left / turn_right / turn_180 → 제자리 회전 → cmd_ack 발행
  ├─ lift_up / lift_down → 리프트 → cmd_ack 발행
  └─ 완료 후 IDLE (다음 명령 대기)
```

#### 바퀴 회전 시각 효과

```python
# forward 이동 중: 이동 거리 기반 누적
self.wheel_angle += abs(v) * dt / WHEEL_RADIUS

# 쿼터니언: 90°X 기본자세 + world-Y 롤링 + heading 방향 반영
q_wheel = q_head(heading) * q_roll(wheel_angle) * q_base(90°X)
```

#### CAD 교체 포인트

```python
# step6_visual.py 상단
CAD_PATHS = {
    "agv":         None,   # USD 경로 입력 시 VisualCuboid 대신 USD 로드
    "shelf":       None,
    "workstation": None,
}
```

### MQTT 스레드 안전성 (step6 핵심 패턴)

MQTT 콜백은 별도 스레드에서 실행되므로, AGV 상태를 직접 수정하면 race condition 발생.
`_pending_cmd` 필드를 통해 main loop에서만 상태를 변경한다.

```python
# MQTT 콜백 (Bridge._on_cmd): pending에 저장만
agv._pending_cmd = msg["cmd"]

# main loop: IDLE 상태일 때만 소비
if agv._pending_cmd is not None and agv.state == "IDLE":
    cmd = agv._pending_cmd
    agv._pending_cmd = None
    agv.execute_cmd(cmd)   # forward/turn_left/turn_right/turn_180/lift_up/lift_down

# execute_cmd 처리:
# - forward   : 현재 heading 방향으로 이동 시작, 마커 감지 시 완료
# - turn_*    : 회전 완료 후 bridge.publish_cmd_ack(cmd) 발행
# - lift_up/down : 리프트 완료 후 bridge.publish_cmd_ack("lift_up"/"lift_down") 발행
```

마커 감지 흐름:
```python
# poll_camera() — main loop에서 호출
(marker_id, heading) = camera.detect()
if marker_id is not None:
    bridge.publish_marker(agv.rid, marker_id, heading)  # → /agv/marker
    # 서버가 위치 갱신 후 다음 명령 발행
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
pip install paho-mqtt pandas openpyxl

# Isaac Sim 의존성 (Isaac Sim Python 환경 내)
# paho-mqtt 별도 설치 필요
```

### 시뮬레이션 실행 (터미널 3개)

```bash
# 터미널 1: AGV 서버
cd TU_Capstone_Design
python3 -m server.main

# 터미널 2: Isaac Sim (AGV 역할)
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py

# 터미널 3: 주문 넣기 (구 mqtt_test.py는 archive로 이동 — GUI가 MQTT로 완전 전환)
mosquitto_pub -h localhost -t warehouse/order/start \
  -m '{"사용자ID":1,"주문번호":1,"작업대":2}'
```

### GUI 포함 전체 실행 (네트워크 필요)

```bash
# 노트북 터미널 1: AGV 서버
cd TU_Capstone_Design && python3 -m server.main

# 노트북 터미널 2: 재고 서버 (최초 1회만 DB 생성)
cd TU_Capstone_Design/warehouse_gui_server
python3 excel_to_sqlite.py     # 최초 1회 (또는 데이터 베이스.xlsx 변경 시)
python3 warehouse_server_v2.py

# 노트북 터미널 3: Isaac Sim
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py

# Raspberry Pi(1번=작업대1): GUI
cd TU_Capstone_Design/warehouse_gui_server && python3 warehouse_gui_ws1.py
# Raspberry Pi(2번=작업대2): GUI
cd TU_Capstone_Design/warehouse_gui_server && python3 warehouse_gui_ws2.py
# -> 시작 시 노트북 핫스팟 IP 입력 팝업 (hostname -I 로 확인). 입력 IP는 ~/.warehouse_gui_ip 에 캐시
```

> ⚠️ **`warehouse_gui_v2.py`는 ws2의 사본**(WORKSTATION_ID=2)이다. 1번 라파에서 이걸 열면
> 주문이 작업대 2로 발행되어 AGV가 반대편으로 간다. **라파별로 ws1/ws2를 열 것.**

### 실물/벤치와 함께 (디지털 트윈)

```bash
# 터미널 1: 서버 / 터미널 2: 트윈
./isaac_simulation/run_twin.sh 3.0

# 터미널 3: 실물 AGV(라파) 또는 벤치 가짜 로봇
python3 -m virtual_test.bench_camera.run_bench 1 --no-camera --auto-walk 8 --ack-delay 3.0
```
상세는 [`../virtual_test/README.md`](../virtual_test/README.md) (3모드), 실물은 [`../hardware/INTEGRATION.md`](../hardware/INTEGRATION.md).

네트워크: 휴대폰 핫스팟에 노트북, Raspberry Pi, AGV 실물 모두 연결.

---

## 10. 하드웨어 구성

### AGV 하드웨어 스택

```
+---------------------------+
|      Raspberry Pi 5       |  상위 제어부
|  - MQTT 수신 (/agv/cmd)   |
|  - 카메라: ArUco 감지     |
|  - UART → STM32 명령      |
+---------------------------+
           | UART 115200bps
+---------------------------+
|          STM32            |  하위 제어부
|  - 모터 드라이버 (바퀴)   |
|  - 리프트 액추에이터      |
|  - 인코더 피드백          |
+---------------------------+
```

### AGV 실물 통신 흐름

```
서버 --MQTT(/agv/cmd)--> RPi --UART--> STM32 --PWM--> 모터/리프트
서버 <-MQTT(/agv/marker, /agv/cmd_ack)-- RPi <-UART-- STM32
카메라 → RPi (OpenCV ArUco) --MQTT(/agv/marker)--> 서버
```

### 하드웨어 (실물 RPi) — 상세는 `hardware/README.md`

> bridge·카메라가 Isaac/실물 2개로 분리됨(2026-06-30): Isaac=`isaac_simulation/bridge_isaac.py`(콜백)+IsaacCamera, 실물=`hardware/bridge_rpi.py`(UART)+RpiCamera.
> **실물 전환·UART 프로토콜·통신유실 대응·설정(`config.py`)은 모두 `hardware/README.md` 가 단일 진실.** (이 문서는 Isaac 시뮬레이션 중심)

---

## 11. 향후 계획

### 지금 상태 (2026-07-14)

시뮬레이션·서버·트윈은 **소프트웨어적으로 끝났다.** 남은 건 전부 **실물에 붙여서 숫자를 재는 일**.

| 항목 | 상태 |
|------|------|
| Step 6/7 (시각 + Kinematic Physics + 트윈) | 완료 — 시뮬 4 시나리오 검증 (06-30) |
| 실물 라파 카메라 → 서버 → 트윈 연동 | 완료 (07-11) |
| 서버 알고리즘 (락 모델·교착·멱등·presence) | 완료 — pytest 100 tests |
| **실물 HIL** | **남음** — 아래 |
| Step 8 (Articulation) | 미작성 (졸업 후) |

### 실물 HIL — 붙이는 날 절차서는 [`../hardware/INTEGRATION.md`](../hardware/INTEGRATION.md)

제일 먼저 확인할 2가지 (**벤치로는 검증이 구조적으로 불가능한 것들**):

1. **turn 좌우 방향(handedness)** — 서버가 `turn_left`를 냈을 때 실물이 어느 쪽으로 도나.
   벤치의 가짜 로봇은 서버와 **같은 회전 규약을 공유**하므로 아무것도 말해주지 않는다.
   차 띄워놓고 한 번 쏴보면 5분, 반대면 부호 하나 뒤집으면 된다.
2. **AGV 초기 방향** — 홈 노드에 **북향(heading 0)으로** 놓아야 한다. 서버·트윈 둘 다 그렇게 가정한다.
   (`TRUST_CAMERA_HEADING` 밸브를 켜면 이 제약이 사라진다 → `HEADING_OFFSET` 실측이 선행)

그 외: 마커=노드번호 바닥 배치(**전부 같은 방향으로**) / 2대 동시 주행.

> ⚠️ **마커 시트는 낱장으로 자를 것.** 한 장에 15~20개가 인쇄돼 있으면 9번을 보여줄 때
> 옆칸 10번도 같이 잡혀서 로봇이 "순간이동"한다. (서버 가드는 수정 62/64로 들어가 있지만,
> 애초에 안 보이게 하는 게 맞다)

> UART 프로토콜·통신유실 대응·`hardware/config.py` 설정은 **`hardware/README.md`가 단일 진실**.

---

#### CAD 파일 완성 후 적용 방법

```python
# step6_visual.py 상단 — 지금은 전부 None (기본 도형 사용)
CAD_PATHS = {
    "agv":         None,   # ← "/path/to/agv.usd" 로 바꾸면 CAD 로드
    "shelf":       None,   # ← "/path/to/shelf.usd"
    "workstation": None,   # ← "/path/to/workstation.usd"
}
```

---

### 장기 (선택적)

| 항목 | 내용 |
|------|------|
| Admin 웹 대시보드 | 실시간 맵 뷰 + 로봇 상태 모니터링 |
| 타임아웃/에러 복구 강화 | 통신유실은 STM `Send_Event` 3회 반복으로 1차 대응됨 |
| 물리 엔진 전환 | Step 8 Articulation — CAD+URDF 기반 (졸업 후) |
| CAD 파일 적용 | STEP → USD 변환 후 `CAD_PATHS`에 경로 입력 (지금은 전부 None=기본 도형) |

---

## 의존성

### 서버 (`server/requirements.txt`)

```
paho-mqtt >= 1.6.0
pandas
openpyxl
websockets >= 10.0    # ← 레거시. WebSocket 핸들러는 archive로 갔고 지금은 안 쓴다
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
