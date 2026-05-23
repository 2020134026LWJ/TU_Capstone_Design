# Dispatch Flow — 한글 흐름도

서버가 주문을 받아 AGV에 명령을 발행하기까지 흐름. 코드 길어졌을 때 어디부터 보면 되는지 위한 cheat sheet.

---

## 1. 주문 수신 → 태스크 분해

| 단계 | 파일:라인 | 메서드 | 하는 일 |
|------|-----------|--------|---------|
| MQTT `agv/algorithm` 수신 | `mqtt_client.py` | `_on_algorithm_msg` | UI에서 `start_order` 등 받음 → request_handler로 전달 |
| 라우팅 | `request_handler.py` `handle_message` | type 별로 분기 | `start_order` → `_handle_start_order` (WorkflowMixin) |
| 주문 분해 | `_workflow_mixin.py` `_handle_start_order` | 엑셀 DB 조회 → 선반 리스트 → 태스크 N개 생성 | `T{user}_{order}_{idx}` 형식 |
| 선반 방문 순서 최적화 | `order_optimizer.py` `optimize_shelf_order` | Nearest Neighbor — AGV 홈 기준 가까운 선반부터 |

## 2. 로봇 배정 → 첫 plan

| 단계 | 파일:라인 | 메서드 | 하는 일 |
|------|-----------|--------|---------|
| 사용가능 로봇 탐색 | `robot_manager.py` `get_available_robot` | IDLE + 같은 WS 홈 우선 |
| 공정 배정 (fair) | `_workflow_mixin.py` `get_next_pending_task_fair` | WS별 활성 로봇 수 적은 쪽 우선 |
| 첫 서브태스크 시작 | `_workflow_mixin.py` `_start_subtask` | `GO_TO_SHELF` → `_plan_and_publish_move(rid, current, shelf_node)` 호출 |

## 3. 경로 계획 + 명령 발행 (★ 핵심)

`_movement_mixin.py` **`_plan_and_publish_move(rid, start, goal, is_forwarding=False)`** — 모든 이동의 진입점.

```
1. Staging 체크 (Point A)
   should_stage(goal, rid) → None 이면 corridor 비어있음, 진입 가능
                          → staging_node 반환이면 corridor 점유 중

   [수정 44 — Dispatch ETA] 점유자 곧 빠질 예정이면 staging 우회 대신 현재 위치 대기
     _should_hold_for_eta() → True면 staging_node = start 로 override
       → add_staged_agv(goal, rid, start) — 큐에 등록만 (실제 이동 X)
       → start == actual_goal → 즉시 도착 처리 → is_staged_agv → "staging_wait"

2. A* 경로 계획 (start_heading + turn_penalty=0.3)
   - excluded_transit: 점유 선반, 다른 AGV planned_path, 정지 차량(IDLE/staging)
   - reserved_nodes: 다른 AGV의 시간 예약 (Cooperative A*)

3. node_path → command_queue 변환
   _path_to_commands(node_path, planning_heading)
   → ['forward', 'turn_left', 'forward', ...]

4. 첫 cmd 발행
   _send_next_command(rid) — 충돌 체크 통과 시 mqtt publish
```

## 4. AGV 마커 도착 → 다음 cmd

`_marker_mixin.py` **`_handle_marker_report(rid, marker_id)`**:

```
1. 위치/heading 업데이트
2. planned_path slide (이미 지나친 노드 제거)
3. 위치 기반 회랑 해제 (check_position_release)
   → 점유자가 corridor 밖으로 이동 시 큐에서 다음 AGV 깨우기
4. _staged_to_ws 체크 — corridor 풀려있고 staging 가는 중이면 즉시 직진 replan
5. is_staged_agv 체크 — staging 대기 중이면 hold (cmd 안 보냄)
   → ★ ETA hold도 이 경로로 처리됨 (staging_node=start이므로)
6. 트리거 마커 체크 (RETURNING_SHELF 상태일 때만)
7. 목표 도착 → _process_arrival → 서브태스크 전환
8. lookahead replan → 없으면 _send_next_command
```

## 5. cmd_ack 처리 (회전/리프트 완료)

`_marker_mixin.py` **`_handle_cmd_ack(rid, cmd)`** — turn_left/right/180, lift_up/down 완료 시.
- 회전 → heading 업데이트 → 다음 cmd 발행
- 리프트 → 선반 carry 상태 전환 → 다음 서브태스크 진입

---

## 핵심 상태 변수 (RequestHandler `__init__`)

| 변수 | 타입 | 용도 |
|------|------|------|
| `_reserved_nodes` | `Dict[int, int]` | forward 목적지 예약 (node → rid) |
| `_blocked_robots` | `Set[int]` | 충돌로 보류 중인 로봇 |
| `_yielded_staging_robots` | `Set[int]` | deadlock yield된 staging 로봇 |
| `_staged_to_ws` | `Dict[int, Tuple[int, int]]` | early-release 대기 (rid → (ws, staging)) |
| `_goal_locked_robots` | `Set[int]` | blocker가 내 goal에 있어 대기 중 |
| `_in_flight_cmds` | `Dict[int, str]` | AGV에 발행했지만 ack 안 온 cmd |

---

## 잘 안 되면 어디부터 보나

| 증상 | 의심 메서드 |
|------|-------------|
| 쓸데없는 우회 (예: row 1 detour) | `_plan_and_publish_move` Point A → `should_stage` → `_should_hold_for_eta` |
| 회랑 안 풀림 | `check_position_release` / `handle_marker_trigger` / `release_corridor_without_trigger` |
| AGV 멈춤 (cmd 안 옴) | `_send_next_command` (`_reserved_nodes` 충돌) / `_blocked_robots` 미해제 |
| 두 로봇 동시 진입 | `_is_safe_to_resume` / `_in_flight_cmds` |
| Staging 후 직진 안 함 | `_staged_to_ws` → `staging_released_proceed` 분기 |

---

## 자주 헷갈리는 용어

| 용어 | 의미 |
|------|------|
| **gateway_node** | corridor 진입로 (W2=17, W1=25) — staging 후 corridor로 들어가는 입구 |
| **staging_node** | 대기 노드 (W2=1, W1=41) — corridor 점유 중일 때 canonical 대기 위치 |
| **trigger_node** | 퇴출 감지 ArUco (W2=10, W1=34) — 점유자가 통과하면 corridor 자동 해제 |
| **corridor_area** | `{ws_node, gateway_node}` — 점유자가 이 안에 있으면 "진입 중" 판정 |
| **is_exiting** | corridor 점유자가 퇴출 시작했음 (mark_exiting 호출 후) — 위치 기반 해제 활성화 |
| **is_forwarding** | 선반 들고 다른 WS로 이동 중 — staging 대신 gateway에서 대기 (멀리 우회 방지) |

---

## 수정 이력 hooks

새 수정사항 추가할 때:
1. `CLAUDE.md` "현재 구현 상태" 섹션에 한 줄 추가
2. 메모리 `MEMORY.md`에 `revision_N.md` 등록
3. 이 파일도 영향 받으면 표/흐름 갱신
