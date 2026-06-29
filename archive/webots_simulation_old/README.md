# AGV Webots Simulation (레거시)

> ⚠️ **상태**: Webots 시뮬레이션은 **레거시**. 현재 메인 시뮬레이터는 Isaac Sim 5.1.0.
>
> Webots 빌드는 v4 검증 완료 시점의 스냅샷으로 참조용으로만 유지.
> 신규 알고리즘/통신 변경(cmd-based 등)은 이 폴더에 반영되지 않을 수 있음.
> 새 개발은 [`../isaac_simulation/`](../isaac_simulation/)에서 진행.

---

## 위치 정리

| 항목 | 경로 |
|------|------|
| AGV 서버 | `../server/` (시뮬레이터 무관) |
| 맵/선반/로봇 설정 JSON | `../server/map.json`, `shelf_config.json`, `robot_config.json` |
| 주문 엑셀 DB | `../server/Database/` |
| 하드웨어 추상화 (시뮬/실물 공용) | `../hardware/` (Bridge, Camera ABC, IsaacMotors) |
| RPi 진입점 | `../hardware/rpi_main.py` |
| Isaac Sim (현재 메인) | `../isaac_simulation/` |
| 알고리즘 플로우차트 | `../FLOWCHART.md` |
| CLI 테스트 도구 | `../mqtt_test.py` |

---

## Webots 시뮬레이션 실행 (참조용)

### 의존성

- Webots R2023b 이상
- Python 3.10+
- Mosquitto MQTT 브로커

```bash
sudo apt install mosquitto
sudo systemctl start mosquitto
pip install -r ../server/requirements.txt
```

### 실행

```bash
# 터미널 1: 서버
cd ..
python3 -m server.main

# 터미널 2: Webots
webots webots_simulation/worlds/warehouse_4x8.wbt

# 터미널 3: CLI 테스트
python3 mqtt_test.py
```

---

## ⚠️ Webots ↔ 현재 서버 호환성 주의

Webots 컨트롤러는 **구 path-based 프로토콜**(`/agv/plan`, `/agv/shelf_cmd`, `/agv/control`, `/agv/arrived`, `/agv/shelf_ack`)로 작성됨.

현재 서버(`../server/`)는 **cmd-based 프로토콜**(`/agv/cmd`, `/agv/marker`, `/agv/cmd_ack`)로 마이그레이션됨.

→ **현 서버와 직접 통신 불가**. Webots를 재가동하려면 컨트롤러를 cmd-based로 마이그레이션하거나, Webots용 구 서버 스냅샷을 사용해야 함.

---

## 디렉토리 구조

```
webots_simulation/
|
+-- controllers/                    # Webots 컨트롤러
|   +-- agv_controller/             # 기본 테스트용 (MQTT 없이 단독 실행)
|   +-- agv_mqtt_controller/        # MQTT + Supervisor + 리프트
|       +-- main.py
|       +-- agv_controller.py       # 메인 AGV 로직 (마커 감지, 리프트, NODE_WAIT)
|       +-- aruco_detector.py
|       +-- mqtt_handler.py
|       +-- navigation.py
|       +-- hardware/               # 구 하드웨어 추상화 (현재는 ../hardware/로 통합)
|
+-- worlds/
|   +-- warehouse_4x8.wbt           # Webots 월드 (KIVA 선반 8개, AGV 2대, ArUco 마커)
|
+-- textures/aruco_markers/         # ArUco 마커 이미지 (Webots + Isaac Sim 공용)
```

---

## 맵 구조 (서버 기준 — 8×6 / 48노드)

> Webots 월드 파일명은 `warehouse_4x8.wbt`로 남아있지만 실제 서버 맵은 **8×6 그리드 48노드**.

```
        Col1    Col2    Col3    Col4    Col5    Col6    Col7    Col8
Row 1     1       2       3       4       5       6       7       8        STG(1)=W2 staging
Row 2   W2(9) TRG(10)    11      12      13      14      15      16
Row 3    17      18    [S1-1] [S1-2]    21    [S1-3] [S1-4]    24         gateway(17)=W2
Row 4    25      26    [S2-1] [S2-2]    29    [S2-3] [S2-4]    32         gateway(25)=W1
Row 5  W1(33) TRG(34)   35      36      37      38      39      40
Row 6    41      42      43      44      45      46      47      48        STG(41)=W1 staging
```

- **W1(33)**: gateway=25, staging=41, trigger=34
- **W2(9)**:  gateway=17, staging=1,  trigger=10
- **AGV-1 home**: 9 (W2) / **AGV-2 home**: 33 (W1)

---

## MQTT 토픽 (Webots 컨트롤러 기준 — 레거시)

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 (구 path-based) |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/control` | Server → AGV | resume 명령 (NODE_WAIT 해제) |
| `/agv/arrived` | AGV → Server | 최종 도착 / 중간 노드 위치 보고 |
| `/agv/shelf_ack` | AGV → Server | 리프트 완료 알림 |
| `/agv/marker` | AGV → Server | ArUco 마커 인식 (트리거 노드 통과) |
| `agv/algorithm` | GUI/CLI → Server | UI 명령 수신 |

> **현재 서버는 cmd-based**: `/agv/cmd` + `/agv/cmd_ack`로 통합됨. 자세한 내용은 `../server/README.md` 참조.

---

## 로봇 상태 머신

```
IDLE -> MOVING_TO_SHELF -> PICKING_UP_SHELF -> DELIVERING_TO_WS -> WAITING_FOR_PICK
                                                                          |
                                              +---------------------------+
                                              |                           |
                                       [다른 WS 필요]                [불필요]
                                              |                           |
                                       FORWARD_SHELF              RETURNING_SHELF
                                              |                           |
                                       WAITING_FOR_PICK          [다음 선반?]
                                                              Yes -> MOVING_TO_SHELF
                                                              No  -> IDLE
```

---

## 폴더별 상세 문서

| 폴더 | README |
|------|--------|
| `../server/` | 서버 모듈, API, 통신 프로토콜, 알고리즘 (현재) |
| `../isaac_simulation/` | Isaac Sim 5.1.0 시뮬레이션 (현재 메인) |
| `../hardware/` | Bridge + Camera ABC + UART 프로토콜 |

알고리즘 플로우차트 및 수정 이력: `../FLOWCHART.md`
검증 체크리스트: `../검증_체크리스트.md`

---

## 필수 패키지 (구 Webots 환경)

| 패키지 | 용도 |
|--------|------|
| `paho-mqtt` | MQTT 클라이언트 |
| `websockets` | WebSocket 서버 (Admin UI용) |
| `mosquitto` | MQTT 브로커 |
| `pandas` | 엑셀 파일 읽기 |
| `openpyxl` | pandas xlsx 엔진 |
