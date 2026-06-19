# AGV 서버

STM32 + Raspberry Pi 기반 AGV 2대가 KIVA 선반을 작업대로 운반하는 물류 피킹 시스템의 중앙 서버.
**AGV는 깡통(sensor+actuator), 서버가 두뇌** — 경로 계획·작업 스케줄링·충돌/교착 회피·MQTT 제어 담당.

## 빠른 시작

```bash
pip install -r requirements.txt          # 의존성
mosquitto -v                             # MQTT 브로커 (별도 터미널)
python3 -m server.main                   # 서버 실행
```

테스트:
```bash
pytest                                   # 회귀 테스트 (프로젝트 루트에서)
python3 mqtt_test.py                     # CLI 주문 시뮬 (시작 / 완료 N / 주문완료 N)
```

## 설계 문서 (정확한 최신 기준)

이 파일은 진입점만 안내합니다. 구조/알고리즘 상세는 아래를 보세요:

| 보고 싶은 것 | 문서 |
|---|---|
| **전체 흐름 한눈에** (이벤트↔핸들러↔다이어그램, 재료, start_order 시퀀스) | `server/main.py` 상단 docstring |
| **주문→cmd 발행 디스패치 흐름** (한글 cheat sheet) | `server/DISPATCH_FLOW.md` |
| **알고리즘 플로우차트 + 수정 이력** (설계 단일 진실) | `../FLOWCHART.md` |
| **경로/예약 재설계 (REFACTOR F) 내역** | `server/REFACTOR_F.md` |
| 각 mixin 메서드 지도 | `_movement_mixin.py` / `_marker_mixin.py` / `_workflow_mixin.py` 상단 docstring |

## 모듈 구조

```
main.py              진입점 (MQTT/WebSocket 서버 시작, 배선)
request_handler.py   메시지 라우터(handle_message) + 상태 변수 + 3 mixin 상속
  _movement_mixin    이동 명령 발행 + 충돌/교착 예방 (★ _plan_and_publish_move)
  _marker_mixin      AGV 이벤트 (marker/cmd_ack/trigger) — 주행 엔진
  _workflow_mixin    주문/태스크/F-노드/인터셉트
reservation_service  미래 점유 단일 진실 (cell/edge/indefinite 시공간 예약)
command_queue        AGV별 cmd lifecycle (in_flight/예약/blocked 추론)
path_planner         A* 시간 기반 경로 (reservation 연동, turn_penalty)
order_optimizer      Nearest Neighbor 선반 방문 순서 최적화
robot_manager        로봇 6단계 상태 머신
shelf_manager        선반 3상태 추적 (IN_PLACE/CARRIED/AT_WORKSTATION)
staging_manager      회랑 점유(=reservation 파생) + 대기 큐 + 트리거
task_manager         주문 → 서브태스크 시퀀스 분해
mqtt_client          MQTT publish/subscribe
websocket_handler    WebSocket 서버 (작업대 UI)
db_loader            엑셀 주문/재고 로더 (warehouse_gui_server/ 공유)
```

> 주문/재고 데이터는 협업자 GUI와 공유하는 `../warehouse_gui_server/` (xlsx)에서 읽습니다.
