# Isaac Sim 이전 작업 (진행 중)

Webots 시뮬레이션을 Isaac Sim 5.1.0으로 이전하는 작업 디렉토리.

> `server/`, `config/`, `Database/` 는 **건드리지 않음** — MQTT 기반이라 시뮬레이터 무관.

## 현재 단계

| 단계 | 내용 | 상태 |
|------|------|------|
| **Step 1** | 환경 확인 + 8×4 창고 씬 레이아웃 | ✅ `hello_world.py` |
| Step 2 | 8×4 그리드 씬 완성 (바닥 마커, 3층 선반, 작업대) | ✅ `step2_environment.py` |
| Step 3 | AGV 이동 + MQTT 연동 | 🔲 |
| Step 4 | 리프트 + 선반 이동 (USD stage API) | 🔲 |
| Step 5 | ArUco 마커 (Camera 센서 + OpenCV) | 🔲 |

## 실행 방법

```bash
cd ~/Desktop/Projects/TU_Capstone_Design/isaac_simulation

~/isaacsim/_build/linux-x86_64/release/python.sh hello_world.py
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

## Webots → Isaac Sim 변환 메모

| Webots | Isaac Sim |
|--------|-----------|
| `.wbt` 월드 파일 | USD 씬 (`.usd`) |
| `getFromDef("SHELF_11")` | `stage.GetPrimAtPath("/World/Shelf_11")` |
| `robot.step(timestep)` | `world.step(render=True)` |
| `hardware/webots_hw.py` | `hardware/isaac_hw.py` (향후 작성) |
| `navigation.py` (속도 직접 제어) | `WheeledRobot` + `DifferentialController` |
| Supervisor 모드 | Articulation / USD API |
