# Worlds

Webots 시뮬레이션 월드 파일 모음

## 파일 목록

| 파일 | 설명 |
|------|------|
| `warehouse.wbt` | 기본 창고 월드 (테스트용) |
| `warehouse_9x5.wbt` | 9x5 그리드 창고 월드 (메인) |

## 월드 설명

### `warehouse_9x5.wbt` (메인)
- 9x5 그리드 맵 (45개 노드)
- AGV 2대 배치 (AGV-1, AGV-2)
- map.json과 동일한 노드 배치

```
그리드 배치:
 1  2  3  4  5  6  7  8  9
10 11 12 13 14 15 16 17 18
19 20 21 22 23 24 25 26 27
28 29 30 31 32 33 34 35 36
37 38 39 40 41 42 43 44 45
```

## 실행 방법

```bash
webots worlds/warehouse_9x5.wbt
```

또는 Webots GUI에서 File > Open World
