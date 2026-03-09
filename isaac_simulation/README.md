# Isaac Sim 이전 작업 (진행 중)

Webots 시뮬레이션을 Isaac Sim 5.1.0으로 이전하는 작업 디렉토리.

> `server/`, `config/`, `Database/` 는 **건드리지 않음** — MQTT 기반이라 시뮬레이터 무관.

## 현재 단계

| 단계 | 내용 | 상태 |
|------|------|------|
| **Step 1** | 환경 확인 + 8×4 창고 씬 레이아웃 | ✅ `hello_world.py` |
| **Step 2** | 8×4 그리드 씬 완성 (바닥 마커, 3층 선반, 작업대) | ✅ `step2_environment.py` |
| **Step 3** | AGV 이동 + MQTT 연동 (바퀴2개 + 시저리프트) | ✅ `step3_agv_mqtt.py` |
| **Step 4** | 리프트 + 선반 이동 (USD stage API) | 🔶 `step4_lift_shelf.py` (버그 수정 완료, 검증 필요) |
| Step 5 | ArUco 마커 (Camera 센서 + OpenCV) | 🔲 |

## 실행 방법

```bash
# ⚠️ 절대 경로 사용 필수 (상대 경로로 실행 시 파일 못 찾음)
~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step4_lift_shelf.py
```

## Step 1 — hello_world.py

기존 `config/map.json`, `config/shelf_config.json`, `config/robot_config.json`을 읽어서
8×4 창고 레이아웃을 Isaac Sim 씬으로 표시.

**색상 구분:**

| 색상 | 의미 |
|------|------|
| 노란색 | 선반 노드 (11,12,14,15,19,20,22,23) |
| 초록색 | 작업대 W1(33), W2(34) |
| 주황색 | 스테이징 노드 (9, 17) |
| 보라색 | ArUco 트리거 노드 (2, 26) |
| 회색   | 일반 통로 노드 |
| 빨간색 | AGV-1 초기 위치 |
| 파란색 | AGV-2 초기 위치 |

## Step 3 완료 내용 (step3_agv_mqtt.py)
- 씬: 3층 선반 + 작업대 + ArUco 바닥 마커
- AGV 외형: 바디 + 바퀴 2개 + 시저리프트(X자 막대 + 상판)
- 이동: 선형 보간, 노드 간 MOVING → NODE_WAIT → resume
- MQTT: `/agv/plan` 수신 → 경로 추종 / `/agv/arrived`, `/agv/marker` 발행

## Step 4 구현 내용 및 버그 수정 이력 (step4_lift_shelf.py)
- `/agv/shelf_cmd` pickup → 시저리프트 상판 올리기 + 선반 attach
- `/agv/shelf_cmd` putdown → 상판 내리기 + 선반 원위치 해제
- `/agv/shelf_ack` 발행

### 수정된 버그
| 버그 | 원인 | 수정 |
|------|------|------|
| 대각선 이동 | 이동 중 plan 수신 시 현재 위치→목표 직선 | `_handle_plan()`에서 `agv.pos = node_xy(node_path[0])` 스냅 |
| 선반 원점 쏠림 | `world.reset()`이 루트 translate 초기화 | **delta 방식**: 루트 translate 없음, 이동 시 `dx/dy = agv.pos - orig` 계산 |
| 선반 허공 이동 | 루트 prim에 translate Op 없음 | delta 방식으로 해결 |

### delta 방식 핵심
```python
shelf_origins[node_id] = (x, y)  # 원래 위치 저장
# build_shelf: 루트 Xform에 translate 없음 (자식 prim은 절대좌표)
# _sync_prim: 이동 시 dx/dy = agv.pos - orig → 루트에 SetTranslateOp
```

## Webots → Isaac Sim 변환 메모

| Webots | Isaac Sim |
|--------|-----------|
| `.wbt` 월드 파일 | USD 씬 (`.usd`) |
| `getFromDef("SHELF_11")` | `stage.GetPrimAtPath("/World/Shelf_11")` |
| `robot.step(timestep)` | `world.step(render=True)` |
| `hardware/webots_hw.py` | `hardware/isaac_hw.py` (향후 작성) |
| `navigation.py` (속도 직접 제어) | `WheeledRobot` + `DifferentialController` |
| Supervisor 모드 | Articulation / USD API |
