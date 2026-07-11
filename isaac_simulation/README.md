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
|   +-- step6_visual.py         # Step 6: 바퀴/차체방향/시각/터치스크린 (현재 메인)
|   +-- step7_kinematic.py      # Step 7: Kinematic Physics (구현 완료, 런타임 검증 남음)
|   +-- bridge_isaac.py         # Isaac 전용 bridge (콜백)
|   +-- isaac_hw.py             # IsaacMotors (가상)
|   +-- camera.py               # IsaacCamera (proximity 가상 마커 감지)
|   +-- STEP8_PLAN.md / README.md
|
+-- hardware/                   # AGV 실물(RPi) 전용 (주원이 STM 펌웨어 대응)
|   +-- rpi_main.py             # 실물 진입점 (camera+bridge, AGV_ID로 rid)
|   +-- bridge_rpi.py           # MQTT ↔ UART 브릿지
|   +-- camera.py               # RpiCamera (주원이 opencv ArUco 비전)
|   +-- config.py               # 배포 설정 (서버IP/UART/카메라)
|   +-- stm32/rpi_uart.c, AGV_Control.zip   # 주원이 STM 소스/펌웨어
|
+-- server/                     # AGV 서버 (MQTT 기반) — 2026-06-13 레이어 분리
|   +-- main.py  config.py
|   +-- comm/ core/ planning/ managers/ data/ docs/   # 상세는 루트 README / CLAUDE.md
|
+-- virtual_test/               # 실물 없이 도는 테스트 (algorithm=pytest, software_in_the_loop=SIL)
|
+-- webots_simulation/          # Webots 전용
|   (controllers, worlds, textures)
|
+-- mqtt_test.py                # CLI 테스트 도구
+-- FLOWCHART.md                # 알고리즘 플로우차트 + 수정 이력
|
+-- warehouse_gui_server/       # 작업자 GUI + 재고 서버
|   +-- warehouse_gui_v2.py     # KivyMD 터치스크린 GUI (Raspberry Pi, IP 입력 팝업)
|   +-- warehouse_server_v2.py  # Flask HTTP + MQTT + SQLite 재고 관리
|   +-- excel_to_sqlite.py      # 데이터 베이스.xlsx -> warehouse.db (최초 1회)
|   +-- 사용자{1,2}주문.xlsx    # 주문 데이터
|   +-- 데이터 베이스.xlsx      # 재고 마스터
|
+-- archive/                    # 이전 버전 (v1~v3, 참조용)
+-- README.md
```

---

## 3. 맵 구조

```
          1 - 2 - 3 - 4 - 5 - 6 - 7 - 8    (row 0, 통로)
          |   |   |   |   |   |   |   |
W2(9)---  9 -10 -11 -12 -13 -14 -15 -16    (row 1, W2 작업대)
          |   |   |   |   |   |   |   |
         17 -18 -[19]-[20]-21-[22]-[23]-24   (row 2, []=선반)
          |   |   |   |   |   |   |   |
         25 -26 -[27]-[28]-29-[30]-[31]-32   (row 3, []=선반)
          |   |   |   |   |   |   |   |
W1(33)-- 33 -34 -35 -36 -37 -38 -39 -40    (row 4, W1 작업대)
          |   |   |   |   |   |   |   |
         41 -42 -43 -44 -45 -46 -47 -48    (row 5, 통로)
```

| 항목 | 값 |
|------|----|
| 전체 노드 | 48개 (8×6 그리드) |
| 선반 노드 | 8개 — 19, 20, 22, 23, 27, 28, 30, 31 |
| 작업대 | W1=33 (user_id=1), W2=9 (user_id=2) |
| W1 회랑 | gateway=25, staging=41, trigger=34 (수정 28: staging 25→41) |
| W2 회랑 | gateway=17, staging=1,  trigger=10 (수정 28: staging 17→1) |
| AGV 홈 | AGV-1: node 9 (W2), AGV-2: node 33 (W1) |
| 노드 타입 | M(통로), S(선반), W(작업대) |

---

## 4. 통신 프로토콜

### MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/cmd` | Server → AGV | 이동/회전/리프트 명령 |
| `/agv/marker` | AGV → Server | ArUco 마커 감지 (위치 + heading) |
| `/agv/cmd_ack` | AGV → Server | 회전/리프트 완료 알림 |
| `warehouse/order/start` `warehouse/shelf/complete` `warehouse/order/complete` | GUI → Server | 주문 시작/선반완료/주문종료 (start_order 등) |
| `warehouse/shelf/arrived` | Server → GUI | AGV 작업대 도착 알림 (선반 셀 활성화) |

### 메시지 형식

서버 → AGV 명령 (`/agv/cmd`):
```json
{"rid": 1, "cmd": "forward"}
{"rid": 1, "cmd": "turn_left"}
{"rid": 1, "cmd": "turn_right"}
{"rid": 1, "cmd": "turn_180"}
{"rid": 1, "cmd": "lift_up"}
{"rid": 1, "cmd": "lift_down"}
```

AGV → 서버 위치 보고 (`/agv/marker`):
```json
{"rid": 1, "marker_id": 14, "heading": 90, "ts": 1700000000}
```
- `heading`: 0=North, 90=East, 180=South, 270=West

AGV → 서버 완료 알림 (`/agv/cmd_ack`):
```json
{"type": "cmd_ack", "rid": 1, "cmd": "turn_left", "status": "done"}
{"type": "cmd_ack", "rid": 1, "cmd": "lift_up",   "status": "done"}
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
  +-- _handle_start_order()      : 주문 시작 -> DB 로드 -> 선반 배정 -> 명령 발행
  +-- _handle_shelf_complete()   : 선반 피킹 완료 -> 반납 or 포워딩 결정
  +-- _handle_marker_report()    : /agv/marker 수신 -> 위치 갱신 -> 다음 cmd 발행
  +-- _handle_cmd_ack()          : /agv/cmd_ack 수신 -> turn/lift 완료 처리
  +-- _handle_putdown_ack()      : lift_down 완료 -> 다음 선반 or IDLE
  +-- _send_next_command()       : forward 충돌 체크 + _reserved_nodes 예약
  +-- _retry_blocked_robots()    : blocked 재시도 + 교착 감지
  +-- _resolve_deadlock()        : 우선순위 결정 + 우회/yield 전략
  +-- _is_staging_robot()        : staging 큐 멤버십 (수정 28)
  +-- _plan_and_publish_move()   : STG 체크 포함 경로 계획
  +-- _try_assign_pending_tasks(): PENDING 작업 재배정 루프

task_manager.py
  +-- 서브태스크 시퀀스 생성 (GO_TO_SHELF -> PICKUP -> DELIVER -> WAIT -> RETURN)
  +-- handle_shelf_complete()    : 선반 단위 완료 처리
  +-- rotate_shelf_to_end()      : 블록 선반을 순서 뒤로

order_optimizer.py
  +-- Nearest Neighbor 알고리즘으로 선반 방문 순서 최적화 (구 task_scheduler)

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

```
AGV가 forward 직전:
  서버 _send_next_command() 충돌 체크
    +-> next_node 점유/예약 -> _blocked_robots 추가 -> 명령 보류
    +-> 안전 -> _reserved_nodes[next_node]=rid 예약 + forward 발행

AGV가 마커 도착:
  /agv/marker 발행 -> 서버 위치 갱신 + _reserved_nodes 해제
  _retry_blocked_robots() -> 보류 중인 다른 AGV 재시도
    +-> blocker도 blocked OR staging -> _resolve_deadlock() (수정 28)
```

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
| Step 6 | 바퀴 회전 + 터치스크린 + 선반 orient 유지 (코드 완료) | 🔲 런타임 검증 필요 |

### 실행 방법

```bash
# 절대 경로 필수 (현재 최신: step6_visual.py)
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step6_visual.py
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
pip install paho-mqtt websockets pandas openpyxl

# Isaac Sim 의존성 (Isaac Sim Python 환경 내)
# paho-mqtt 별도 설치 필요
```

### 시뮬레이션 실행 (터미널 3개)

```bash
# 터미널 1: AGV 서버
cd TU_Capstone_Design
python3 -m server.main

# 터미널 2: Isaac Sim 시뮬레이션
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step6_visual.py

# 터미널 3: CLI 테스트
cd TU_Capstone_Design
python3 mqtt_test.py
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
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step6_visual.py

# Raspberry Pi: GUI
cd TU_Capstone_Design/warehouse_gui_server
python3 warehouse_gui_v2.py
# -> 시작 시 노트북 핫스팟 IP 입력 팝업 (hostname -I 로 확인). 입력 IP는 ~/.warehouse_gui_ip 에 캐시
```

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

### 발표 전 (우선순위)

| 순서 | 항목 | 내용 |
|------|------|------|
| 1 | **Step 6 검증** | TURNING/MOVING 동작, 바퀴 회전, 센서/LED 시각 확인 |
| 2 | **전체 흐름 시연** | 서버 + Isaac Sim + GUI 연동 영상 |

### 발표 후 — 하드웨어 연동 (2026-06-30 코드 통합 완료)

> ⚠️ 아래는 옛 계획(0xAA 바이너리 패킷)이었고, **실제는 주원이 STM 프로토콜로 확정**됨. 상세·최신은 **`hardware/README.md`**.

핵심만:
- **UART 송신**: ASCII `<cmd,±xxxx,±yyyy,±wwww>` 21바이트 (cmd 1~7, x/y/yaw=ArUco offset×10)
- **명령 코드**: forward1 / stop2 / lift_up3 / lift_down4 / turn_left5 / turn_right6 / turn_180_7
- **수신**: 단일바이트 `0x81`=DONE / `0xFF`=ACK (각 3회 반복 송신 — 신호 유실 대비)
- **forward 완료 = 카메라 마커**(서버 ACK), turn/lift 완료 = cmd_ack
- **실행**: `python3 -m hardware.rpi_main` (라파별 `export AGV_ID=1/2`), 설정은 `hardware/config.py` (`UART_ENABLED`/`MQTT_HOST` 등)
- **서버 코드 변경 없음** — `/agv/cmd` `/agv/marker` `/agv/cmd_ack` 토픽 동일
- **남은 HIL**: heading K 보정 / turn 좌우 방향 / 마커=노드 배치

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

#### 전체 하드웨어 연동 순서

| 순서 | 항목 | 구체적 작업 | 난이도 |
|------|------|------------|--------|
| 1 | UART 프로토콜 연동 | STM32 팀과 패킷 포맷 최종 확인 | 낮음 |
| 2 | config.py 설정 | UART_ENABLED=True, MQTT_HOST=서버IP (hardware/config.py) | 낮음 |
| 3 | RPi 실행 확인 | main.py 1/2 실행 → MQTT ↔ UART 동작 | 낮음 |
| 4 | 카메라 캘리브레이션 | camera_calibration.pkl 생성 | 중간 |
| 5 | RpiCamera ArUco 실물 | 마커 감지 + heading 정확도 확인 | 중간 |
| 6 | 리프트 타이밍 조정 | lift_up/down 완료 타이밍 튜닝 | 중간 |
| 7 | CAD 파일 적용 | STEP → USD, CAD_PATHS 입력 | 낮음 |

### 장기 (선택적)

| 항목 | 내용 |
|------|------|
| Admin 웹 대시보드 | 실시간 맵 뷰 + 로봇 상태 모니터링 |
| 타임아웃/에러 복구 | AGV 응답 없음 시 재시도 + 경보 로직 |
| 물리 엔진 전환 | CAD+URDF 기반 Articulation (발표 이후) |

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
