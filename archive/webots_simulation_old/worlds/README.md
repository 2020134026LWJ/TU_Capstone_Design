# Worlds

Webots 시뮬레이션 월드 파일 모음

## 파일 목록

| 파일 | 설명 |
|------|------|
| `warehouse_4x8.wbt` | **현재 사용** — 8×4 그리드, KIVA 선반 8개, AGV 2대, ArUco 마커 |
| `warehouse_7x7.wbt` | 구버전 (v4 초기) — 7×7 그리드, 9개 선반 |
| `warehouse_9x5.wbt` | 구버전 (v3) — 9×5 그리드 |

## warehouse_4x8.wbt (현재 월드)

### 맵 구조 (8×4 + 작업대 2개)

```
W1(33)── 1 ─ 2 ─ 3 ─ 4 ─ 5 ─ 6 ─ 7 ─ 8    (row 0, 통로)
         |   |   |   |   |   |   |   |
         9 ─10 ─[11]─[12]─13 ─[14]─[15]─16   (row 1, []=선반)
         |   |   |   |   |   |   |   |
        17 ─18 ─[19]─[20]─21 ─[22]─[23]─24   (row 2, []=선반)
         |   |   |   |   |   |   |   |
W2(34)──25 ─26 ─27 ─28 ─29 ─30 ─31 ─32    (row 3, 통로)

W1(33): gateway=1, staging=9, trigger=2
W2(34): gateway=25, staging=17, trigger=26
```

- **총 노드**: 34개 (32 그리드 + 2 작업대)
- **M (통로)**: 26개 — 로봇 이동 경로
- **S (선반)**: 8개 — 11, 12, 14, 15, 19, 20, 22, 23
- **W (작업대)**: 2개 — 33(W1), 34(W2)

### KIVA 3D 선반

각 선반은 DEF 노드로 정의 (예: `DEF SHELF_11`, `DEF SHELF_12`, ..., `DEF SHELF_23`)

**선반 구조:**
- 4개 다리 (Cylinder) + 선반판 (Box)
- 각 선반에 복수 물품 (3~4종/선반)
- Supervisor API로 translation 필드를 제어하여 AGV와 함께 이동

**DEF 이름 매핑:**

| DEF 이름 | 노드 ID | 선반 라벨 |
|----------|---------|-----------|
| SHELF_11 | 11 | 1-1 |
| SHELF_12 | 12 | 1-2 |
| SHELF_14 | 14 | 1-3 |
| SHELF_15 | 15 | 1-4 |
| SHELF_19 | 19 | 2-1 |
| SHELF_20 | 20 | 2-2 |
| SHELF_22 | 22 | 2-3 |
| SHELF_23 | 23 | 2-4 |

### ArUco 마커

- 각 작업대 트리거 위치에 ArUco 마커 배치
- W1 트리거: 노드 2 (마커 ID로 구분)
- W2 트리거: 노드 26 (마커 ID로 구분)
- `worlds/textures/aruco_markers/` — 마커 이미지 파일

### AGV (Pioneer3dx 기반)

- 2대 배치: AGV-1 (W1, 노드 33), AGV-2 (W2, 노드 34)
- 컨트롤러: `agv_mqtt_controller` (Supervisor 모드)
- `extensionSlot`에 리프트 메커니즘 추가:
  - **SliderJoint** + **LinearMotor**: 수직 이동 (선반 들어올리기/내려놓기)
  - 리프트 범위: 0.0 ~ 0.15m

## 실행 방법

```bash
webots worlds/warehouse_4x8.wbt
```

또는 Webots GUI에서 File > Open World
