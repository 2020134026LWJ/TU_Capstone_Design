# AGV 서버

STM32 + Raspberry Pi 기반 AGV 2대가 KIVA 선반을 작업대로 운반하는 물류 피킹 시스템의 중앙 서버.
**AGV는 깡통(sensor+actuator), 서버가 두뇌** — 경로 계획·작업 스케줄링·충돌/교착 회피·MQTT 제어 담당.

## 빠른 시작

```bash
pip install -r requirements.txt          # 의존성
mosquitto -v                             # MQTT 브로커 (별도 터미널)
python3 -m server.main                   # 서버 실행 (프로젝트 루트에서)
```

주문 넣기 (셋 중 하나):

```bash
# 1) 라파 GUI — 실제 경로 (1번 라파=warehouse_gui_ws1.py / 2번 라파=warehouse_gui_ws2.py)
# 2) 직접 발행 — 가장 빠름
mosquitto_pub -h localhost -t warehouse/order/start -m '{"사용자ID":1,"주문번호":1,"작업대":2}'
# 3) 벤치 하네스 — 로봇까지 가짜로 굴리기 → ../../virtual_test/README.md
```

테스트:
```bash
pytest                                   # 알고리즘 회귀 (100 tests, 프로젝트 루트에서)
```

> 구 `mqtt_test.py` CLI는 GUI가 MQTT로 완전 전환되면서 `archive/`로 이동했다 (2026-06-19).

## 설계 문서 (정확한 최신 기준)

이 파일은 진입점만 안내합니다. 구조/알고리즘 상세는 아래를 보세요:

| 보고 싶은 것 | 문서 |
|---|---|
| **전체 흐름 한눈에** (이벤트↔핸들러↔다이어그램, 재료, start_order 시퀀스) | `server/main.py` 상단 docstring |
| **주문→cmd 발행 디스패치 흐름** (한글 cheat sheet) | [`DISPATCH_FLOW.md`](DISPATCH_FLOW.md) |
| **알고리즘 플로우차트 + 수정 이력** (설계 단일 진실) | [`../../FLOWCHART.md`](../../FLOWCHART.md) |
| **왜 이 구조인가** (노드 락 모델, 평문 설계 노트) | [`../../../설계_근본해결_노트.md`](../../../설계_근본해결_노트.md) — 저장소 밖 상위 폴더(`Projects/`) |
| 경로/예약 재설계(REFACTOR F) 내역 — *완료된 작업 기록* | [`REFACTOR_F.md`](REFACTOR_F.md) |
| 각 mixin 메서드 지도 | `_movement_mixin.py` / `_marker_mixin.py` / `_workflow_mixin.py` 상단 docstring |

## 모듈 구조

> 2026-06-13 레이어 분리. import는 패키지 경로: `from server.managers.robot import RobotManager`
> 의존 방향은 한쪽 — **core → planning/managers/comm/data**. planning/managers는 core를 import하지 않는다.

```
main.py                  진입점 (MQTT 구독 배선 + 이벤트 루프)
config.py                설정 단일 출처 (MQTT 토픽/호스트, data/ JSON 경로, A* 파라미터)

core/                    두뇌 — 이벤트 받아 결정하고 명령 낸다
  request_handler.py       메시지 라우터(handle_message) + 상태 변수 + 3 mixin 다중상속
  _movement_mixin.py       ★ 이동 명령 발행 + 충돌/교착 회피 (_plan_and_publish_move)
  _marker_mixin.py         AGV 이벤트 (marker / cmd_ack / presence / trigger) — 주행 엔진
  _workflow_mixin.py       주문/태스크/F-노드/인터셉트

planning/                도구 — 스스로 결정하지 않는다. core가 가져다 쓴다
  path_planner.py          A* 시간 기반 경로 (reservation 연동, turn_penalty=0.3)
  reservation_service.py   미래 점유 단일 진실 (시공간 예약) — 충돌/교착 예방의 핵심
  deadlock_detector.py     wait-for 사이클 감지 (순수 함수) — 예약이 못 막는 교착 backstop
  order_optimizer.py       Nearest Neighbor 선반 방문 순서 최적화
  command_queue.py         AGV별 cmd lifecycle (in_flight 단일 슬롯)

managers/                도메인 상태 — 무엇이 있나
  robot.py                 로봇 6단계 상태 머신 + presence(online/ever_seen)
  shelf.py                 선반 3상태 (IN_PLACE / CARRIED / AT_WORKSTATION)
  staging.py               회랑 점유 + 대기 큐 + 트리거 (STG 게이팅)
  task.py                  주문 → 서브태스크 시퀀스 분해

comm/mqtt_client.py      MQTT publish/subscribe
data/db_loader.py        엑셀 주문/재고 로더 (warehouse_gui_server/ 공유)
data/*.json              map / shelf_config / robot_config
```

## MQTT 토픽 (서버가 실제로 쓰는 것 전부)

| 토픽 | 방향 | type | 핸들러 |
|---|---|---|---|
| `/agv/cmd` | 서버 → AGV | — | 발행 (forward / turn_* / lift_*) |
| `/agv/marker` | AGV → 서버 | `marker_report` | `_handle_marker_report` |
| `/agv/cmd_ack` | AGV → 서버 | `cmd_ack` | `_handle_cmd_ack` (turn/lift 완료) |
| `/agv/presence` | AGV → 서버 | `presence` | `_handle_presence` (retained + LWT, 수정 75) |
| `warehouse/order/start` | GUI → 서버 | `start_order` | `_handle_start_order` |
| `warehouse/shelf/complete` | GUI → 서버 | `shelf_complete` | `_handle_shelf_complete` (Point C) |
| `warehouse/order/complete` | GUI → 서버 | `order_complete` | `_handle_order_complete` |
| `warehouse/shelf/arrived` | 서버 → GUI | — | 발행 (선반 도착 → 셀 활성화) |

> `/agv/pose`는 서버가 아니라 **Isaac 트윈이 구독**한다 (실물 회전 실시간 추종, 수정 68).

## 알아둘 것

- **`DEMO_MODE`** (`core/request_handler.py:43`) — 발표용 토글. `True`면 WS 전담 + 스테이징 비활성.
  기본 `False`(정상). 시연 후 반드시 되돌릴 것.
- **`TRUST_CAMERA_HEADING`** (`config.py`) — 카메라 heading을 제어에 쓸지의 밸브. 기본 `False`(로그만).
  `hardware/config.py`의 `HEADING_OFFSET`을 **실물에서 실측한 뒤에만** 켤 것.
- 주문/재고 데이터는 협업자 GUI와 공유하는 `../../warehouse_gui_server/` (xlsx)에서 읽는다.
