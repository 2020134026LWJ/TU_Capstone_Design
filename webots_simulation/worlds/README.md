# Worlds

Webots 시뮬레이션 월드 파일 모음

## 파일 목록

| 파일 | 설명 |
|------|------|
| `warehouse_7x7.wbt` | **현재 사용** - 7x7 그리드, KIVA 선반 9개, AGV 2대 |
| `warehouse_9x5.wbt` | 구버전 (v3) - 9x5 그리드 |
| `warehouse.wbt` | 기본 창고 (테스트용) |

## warehouse_7x7.wbt (현재 월드)

### 맵 구조 (7x7 + 작업대 2개)

```
W1(50)─ 1   2   3   4   5   6   7     (row 0, 통로)
        8  [9] 10 [11] 12 [13] 14     (row 1, []=선반)
       15  16  17  18  19  20  21     (row 2, 통로)
       22 [23] 24 [25] 26 [27] 28     (row 3, []=선반)
       29  30  31  32  33  34  35     (row 4, 통로)
       36 [37] 38 [39] 40 [41] 42     (row 5, []=선반)
W2(51)─43  44  45  46  47  48  49     (row 6, 통로)
```

- **총 노드**: 51개 (49 그리드 + 2 작업대)
- **M (통로)**: 40개 - 로봇 이동 경로
- **S (선반)**: 9개 - 9, 11, 13, 23, 25, 27, 37, 39, 41
- **W (작업대)**: 2개 - 50(W1), 51(W2)

### KIVA 3D 선반

각 선반은 DEF 노드로 정의 (예: `DEF SHELF_9`, `DEF SHELF_11`, ..., `DEF SHELF_41`)

**선반 구조:**
- 4개 다리 (Cylinder) + 3개 선반판 (Box)
- 3층 선반: 각 층에 1종류 물품 (총 3종/선반)
- 각 선반판에 Text geometry로 물품명 표시
- Supervisor API로 translation 필드를 제어하여 AGV와 함께 이동

**DEF 이름 매핑:**

| DEF 이름 | 노드 ID | 선반 라벨 |
|----------|---------|-----------|
| SHELF_9 | 9 | S1 |
| SHELF_11 | 11 | S2 |
| SHELF_13 | 13 | S3 |
| SHELF_23 | 23 | S4 |
| SHELF_25 | 25 | S5 |
| SHELF_27 | 27 | S6 |
| SHELF_37 | 37 | S7 |
| SHELF_39 | 39 | S8 |
| SHELF_41 | 41 | S9 |

### AGV (Pioneer3dx 기반)

- 2대 배치: AGV-1 (W1, 노드 50), AGV-2 (W2, 노드 51)
- 컨트롤러: `agv_mqtt_controller` (Supervisor 모드)
- `extensionSlot`에 리프트 메커니즘 추가:
  - **SliderJoint** + **LinearMotor**: 수직 이동 (선반 들어올리기/내려놓기)
  - 리프트 범위: 0.0 ~ 0.15m

## 실행 방법

```bash
webots worlds/warehouse_7x7.wbt
```

또는 Webots GUI에서 File > Open World
