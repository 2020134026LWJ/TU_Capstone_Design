"""
RequestHandler — AGV 이벤트 수신 (마커, cmd_ack, marker_trigger) Mixin

AGV → 서버 방향의 이벤트 진입점들. 위치 갱신, heading 갱신, 후속 명령 발행 트리거.

self 상태 접근:
  - self.command_queues (REFACTOR E), self._staged_to_ws
  - self.robot_manager, self.shelf_manager, self.staging_manager
  - self.path_planner, self.task_manager

다른 mixin 호출:
  - MovementMixin: _plan_and_publish_move, _send_next_command, _try_dispatch_all
  - WorkflowMixin: _try_assign_pending_tasks, _process_arrival, _handle_pickup_ack, _handle_putdown_ack
  - Base: _error_response

섹션 맵 (메서드 → 역할 / 다이어그램 노드):
  _handle_marker_report      [핵심] 마커 수신 → 위치갱신 → STG 체크 → 도착(_process_arrival)/다음 cmd
  _handle_cmd_ack            turn/lift 완료 → heading 갱신 / 다음 단계 → _try_dispatch_all
  handle_marker_trigger      [TRG] 트리거 노드 통과 → 대기 AGV 해제 (staging_manager 위임)
  _dispatch_released_agv     회랑 해제로 깨어난 대기 AGV dispatch (도착 미스 시 _staged_to_ws 보류)
  _is_corridor_dispatch_consistent  dispatch 직전 회랑 점유 일관성 sanity (수정 46)
"""

from typing import Any, Dict

from ..managers.robot import RobotStatus


class MarkerMixin:
    """AGV 이벤트 수신 핸들러"""

    # ─── 수정 75: AGV 접속/이탈 (presence) ───

    def _mark_online(self, rid: int) -> None:
        """그 AGV에게서 신호가 왔다 = 붙어있다. 처음 붙었으면 대기 주문을 태워본다."""
        if self.robot_manager.set_presence(rid, True):
            robot = self.robot_manager.get_robot(rid)
            print(f"[RequestHandler] Robot {rid} ONLINE (node {robot.current_node})")
            self._try_assign_pending_tasks()

    def _handle_presence(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """AGV 접속/이탈 보고 (/agv/presence).

        online=True  : 브릿지가 접속하며 발행 (retained)
        online=False : 브로커가 LWT로 대신 발행 = 전원/네트워크가 끊겼다

        이탈해도 **장부에서 지우지 않는다**. MQTT가 끊긴 것이지 로봇이 바닥에서
        사라진 게 아니다 — 몸은 그 칸에 그대로 서 있으므로 A*는 계속 피해 다녀야
        한다(ever_seen 유지). 끊는 것은 '새 태스크 배정'뿐이다.
        """
        rid = data.get("rid")
        online = data.get("online")
        if rid is None or online is None:
            return self._error_response("Missing 'rid' or 'online' in presence")

        rid = int(rid)
        if not self.robot_manager.get_robot(rid):
            return self._error_response(f"Robot {rid} not found")

        if online:
            self._mark_online(rid)
            return {"type": "presence_ack", "success": True, "action": "online"}

        if self.robot_manager.set_presence(rid, False):
            robot = self.robot_manager.get_robot(rid)
            # 수정 85: 끊긴 로봇은 더 이상 회전하지 않는다(ack가 영영 안 옴) → 회전 footprint
            # 해제. 안 풀면 이웃칸이 영구 잠겨 다른 로봇 진입이 막힌다. 몸(current_node)은
            # 장애물로 그대로 유지되므로 안전.
            self.reservation.release_turn_footprint(rid)
            print(f"[RequestHandler] Robot {rid} OFFLINE — 연결 끊김 "
                  f"(node {robot.current_node}, status {robot.status.value}). "
                  f"신규 배정 중단. 몸은 그 자리에 있으므로 장애물로는 유지한다.")
            if robot.current_task_id is not None:
                print(f"[RequestHandler] ⚠ Robot {rid}가 작업 중({robot.current_task_id})에 "
                      f"끊겼다. 이 태스크는 다른 AGV로 넘어가지 않는다(선반을 든 채 멈췄을 수 "
                      f"있다). 재접속해도 in-flight 명령은 재전송되지 않으므로, 복구하려면 "
                      f"서버를 재시작해야 한다. 태스크 인계는 미구현.")
        return {"type": "presence_ack", "success": True, "action": "offline"}

    # ─── 수정 46: corridor release dispatch sanity check ───

    def _is_corridor_dispatch_consistent(self, rid: int, target_ws: int) -> bool:
        """corridor release로 dispatch 발행 전 robot의 현재 task와 목적지 일치 확인.

        staging 큐 정리 누락 등으로 stale 엔트리가 남아 있으면, robot의 실제 task
        목적지와 다른 target_ws로 dispatch가 나가서 위치 cascade off-by-one 유발.

        Returns:
            True: 일관 — dispatch 진행 OK
            False: stale — dispatch 차단 권장
        """
        robot = self.robot_manager.get_robot(rid)
        if robot is None or robot.current_task_id is None:
            print(f"[RequestHandler] Stale corridor release: AGV-{rid} has no current task "
                  f"(target_ws={target_ws})")
            return False
        task = self.task_manager.get_task(robot.current_task_id)
        if task is None:
            return False
        current_st = task.get_current_subtask()
        if current_st is None:
            return False
        if current_st.target_node != target_ws:
            print(f"[RequestHandler] Stale corridor release: AGV-{rid} task target "
                  f"{current_st.target_node} ≠ corridor target {target_ws}, skipping dispatch")
            return False
        return True

    # ─── 마커 인식 처리 (위치 보고 + 다음 명령 결정) ───

    def _dispatch_released_agv(self, released) -> None:
        """회랑 release로 깨어난 대기 AGV를 dispatch (Phase 4.5.4a — 깨우기 단일화).

        check_position_release / handle_marker_trigger 두 경로의 복붙 dispatch를 통합.
        2갈래:
          A. staging_node 미도착 (desync) → _staged_to_ws에 보류 (도착 후 처리)
          B. sanity(수정 46) 통과 → dispatch
        """
        if released is None:
            return
        released_robot = self.robot_manager.get_robot(released.rid)
        if released_robot and released_robot.current_node != released.staging_node:
            self._staged_to_ws[released.rid] = (released.target_ws, released.staging_node)
            print(f"[RequestHandler] Robot {released.rid}: released early "
                  f"(at {released_robot.current_node}), waiting for staging_node {released.staging_node}")
        elif self._is_corridor_dispatch_consistent(released.rid, released.target_ws):
            start = released_robot.current_node if released_robot else released.staging_node
            self._plan_and_publish_move(released.rid, start, released.target_ws)

    def _handle_marker_report(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """AGV 마커 인식 → 위치 갱신 + 스테이징 체크 + 다음 명령 결정"""
        rid = data.get("rid")
        marker_id = data.get("marker_id")
        if rid is None or marker_id is None:
            return self._error_response("Missing 'rid' or 'marker_id'")

        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return self._error_response(f"Robot {rid} not found")

        self._mark_online(rid)   # 수정 75: 말을 걸어왔으면 붙어있는 것 (presence fallback)

        node = int(marker_id)

        # 수정 59: 맵에 없는 마커는 버린다 (실물 오검출 방어).
        # ArUco는 조명·각도·잘린 마커 때문에 엉뚱한 ID를 내뱉는다(실측: 노드 0~47뿐인데 145 검출).
        # 이걸 그대로 믿으면 robot.current_node가 맵 밖 노드가 되어 경로계획·충돌회피가
        # 통째로 무너진다. 위치는 '모르는 값'보다 '직전 값'이 안전하므로 무시가 정답.
        if node not in self.path_planner.nodes:
            print(f"[RequestHandler] Robot {rid}: 맵에 없는 마커 {node} 무시 (오검출)")
            return {"type": "marker_ack", "success": False, "action": "unknown_marker"}

        # 수정 62: 인접성 검사 — 물리적으로 갈 수 없는 곳에서 온 마커는 버린다.
        #
        # 위의 수정 59 필터는 "맵에 있는 번호냐"만 본다. 그런데 ArUco 오검출은 ID 0~249에
        # 흩어지고 우리 유효 노드는 0~47이라, **5번 중 1번은 유효 노드로 위장해 통과한다**.
        # (실측 2026-07-12: 카메라 앞에 아무것도 없는데 37 → 3 → 4 검출. 145는 걸렀지만
        #  37은 못 걸렀다. 서버가 이걸 믿고 로봇을 맵 반대편으로 순간이동시켰다.)
        #
        # 로봇은 한 칸씩 굴러가지 순간이동하지 않는다. 따라서 다음에 올 수 있는 마커는
        # '현재 노드'(아직 안 떠남) 아니면 '이웃 노드'(도착)뿐이다. 통과 가능한 문이
        # 48개에서 3~4개로 줄고, 설령 뚫려도 피해가 한 칸에 그친다.
        #
        # 마커를 놓쳐서 서버가 뒤처진 경우에도 버리는 게 맞다 — 위치는 '모르는 값'보다
        # '직전 값'이 안전하다 (수정 59와 같은 논리).
        if robot.current_node is not None and node != robot.current_node:
            adjacent = self.path_planner.neighbors(robot.current_node)
            if node not in adjacent:
                print(f"[RequestHandler] Robot {rid}: 불가능한 마커 {node} 무시 "
                      f"(현재 {robot.current_node}, 이웃 {adjacent})")
                return {"type": "marker_ack", "success": False, "action": "non_adjacent_marker"}

        # REFACTOR E 3.2: forward의 ACK = 마커. 큐 ack가 in_flight + reservation 한 번에 해제.
        # I4 일치성: in_flight이 forward이고 target_node가 marker와 일치해야 정상.
        queue = self.command_queues.get(rid)
        if queue is not None and queue.in_flight is not None:
            in_flight = queue.in_flight

            # 수정 92: 회전/리프트 in-flight 중 들어온 마커는 무시 (수정 64의 turn/lift 판).
            # 회전·리프트는 cmd_ack로만 완료되고 그동안 로봇은 같은 노드에 머문다. 그 사이 들어온
            # 마커는 유령(회전 중 옆칸 오검출)이거나 같은 노드 중복 → ack하면 회전을 조기 종료하고
            # 위치를 점프시켜 경로가 통째로 어긋난다(실측 2026-07-21: 회전 중 마커 17 → 노드 25로 이탈).
            # forward만이 마커로 완료된다 — 완료 신호를 in-flight 종류별로 분리한다.
            if in_flight.cmd in ("turn_left", "turn_right", "turn_180",
                                 "lift_up", "lift_down"):
                print(f"[RequestHandler] Robot {rid}: {in_flight.cmd} 중 마커 {node} 무시 "
                      f"(회전/리프트는 cmd_ack로만 완료)")
                return {"type": "marker_ack", "success": False, "action": "marker_during_turn_ignored"}

            # 수정 64: forward 중이면 도착할 수 있는 곳은 **목표 노드 하나뿐**이다.
            #
            # 예전엔 불일치를 감지하고도 WARN만 찍고 **그대로 믿었다**. 실측(2026-07-12):
            # 서버가 "17번으로 forward"를 명령해둔 상태에서 마커 10이 들어오자
            # 로봇을 10번으로 옮기고 거기서 경로를 다시 짰다. AGV가 한 칸 순간이동한 셈.
            # (원인은 마커 시트 — A4 한 장에 15~20개가 격자로 인쇄돼 있어 9번을 보여줄 때
            #  옆칸 10번이 같이 잡혔다. camera.py는 여러 개면 첫 번째를 반환한다.)
            #
            # 수정 62(인접성)보다 강하다: 통과 가능한 문이 '이웃 3~4개' → '목표 1개'.
            # 실물에서도 마커를 놓치거나 잘못 읽으면 같은 사고가 나므로 시트와 무관하게 필요.
            # 위치는 '틀린 값'보다 '직전 값'이 안전하다 (수정 59/62와 같은 논리).
            if in_flight.cmd == "forward" and in_flight.target_node != node:
                print(f"[RequestHandler] Robot {rid}: 목표와 다른 마커 {node} 무시 "
                      f"(forward 목표={in_flight.target_node})")
                return {"type": "marker_ack", "success": False, "action": "marker_target_mismatch"}

            queue.ack()
        self.robot_manager.update_robot_position(rid, node)

        # heading 업데이트: 마커 메시지에 포함된 경우 우선 사용 (ArUco 포즈 기반)
        # 없으면 경로 기반 계산 (이전 방식 폴백)
        # 실물(옵션 a): 카메라 yaw는 절대방위가 아니라 heading 미전송 → 경로 기반 계산.
        # 시뮬: IsaacCamera가 실제 heading 전송 → 그대로 사용.
        reported_heading = data.get("heading")
        if reported_heading is not None:
            robot.heading = int(reported_heading)
        else:
            robot.heading = self.path_planner.calc_heading_from_path(
                robot.planned_path, node
            ) or robot.heading

        # 수정 69 (A 배관) — 실물 카메라 heading은 **보고만 받고 믿지 않는다.**
        #
        # 카메라가 절대방위를 줄 수 있다는 건 실측으로 확인했다(시계방향=yaw 증가, 서버와 같은 부호).
        # 그런데 변환 상수 HEADING_OFFSET은 **실물에서 재야** 확정된다 (카메라 장착각 + 바닥 마커
        # 방향으로 정해짐). 상수가 틀린 채로 제어에 쓰면 **서버가 매 마커마다 방향을 잘못 갱신하며
        # 계속 엉뚱한 명령을 낸다** — 지금의 dead reckoning보다 나빠진다.
        #
        # → 배관은 깔되 밸브는 잠가둔다. 차이를 로그로만 찍는다.
        #   실물에서 이 로그의 차이가 일정하면 그게 곧 HEADING_OFFSET 보정값이다.
        #   **캘리브레이션이 '코드 짜기'가 아니라 '로그 읽기'가 된다.**
        #   확정되면 Config.TRUST_CAMERA_HEADING = True 로 밸브를 연다.
        observed = data.get("heading_observed")
        if observed is not None:
            observed = int(observed)
            if getattr(self.config, "TRUST_CAMERA_HEADING", False):
                robot.heading = observed
            elif observed != robot.heading:
                diff = (observed - robot.heading) % 360
                print(f"[heading] Robot {rid}: 카메라 {observed}° vs 장부 {robot.heading}° "
                      f"— 차이 {diff}° (제어엔 미반영. 차이가 일정하면 그게 HEADING_OFFSET 보정값)")
        # 첫 마커 = heading 확인 완료. heading 출처(보고/경로)와 무관하게 가용 게이트 해제.
        # (이 블록이 reported_heading 분기 안에 있으면 옵션 a에서 영영 False → 배차 안 됨)
        if not robot.heading_initialized:
            robot.heading_initialized = True
            print(f"[RequestHandler] Robot {rid}: heading initialized to {robot.heading}° (first marker)")
            self._try_assign_pending_tasks()

        # planned_path slide: 이미 지나친 노드 제거 → A* 시간 예약을 실제 진행과 정합
        # (heading 계산이 prev_node를 보므로 위 fallback 뒤에서 실행)
        if node in robot.planned_path:
            idx = robot.planned_path.index(node)
            robot.planned_path = robot.planned_path[idx:]
            # REFACTOR E 3.5: 마지막 노드 도달 = path 완료 → 비움 (invariant 정착).
            # 수정 48의 `not r.planned_path` 가드가 parking 종료 후 자동 해제되도록 함
            # (이전엔 [last_node] 한 개 남아서 영구 가드 작동 → 시뮬 멈춤 버그).
            if len(robot.planned_path) <= 1:
                robot.planned_path = []

            # 수정 74: planned_path만 자르고 예약은 안 잘라서, "이미 지나온 칸"이 계속
            # 예약된 채 남아 있었다 (위 주석의 '시간 예약을 실제 진행과 정합'이 실제로는
            # 안 되고 있었음). commit()은 재계획할 때만 옛 예약을 갈아엎으므로, 로봇이
            # 한 경로를 재계획 없이 끝까지 걸으면 그 경로 전체가 예약된 채 남는다.
            # → 다른 AGV의 A*가 죽은 칸을 피해 우회 (실측 6/68건).
            #
            # 남은 경로를 t=0부터 다시 예약 = (a) 지나간 칸 반납 (b) 시간축을 '지금'으로
            # 재정렬. 수정 55가 지운 "매 계획마다 모든 로봇 예약 재동기화" 블록이 하던
            # 두 가지 일을, 실제로 움직인 로봇 자신에 대해서만 되살린 것.
            # edge(스왑 충돌 차단)도 남은 경로에 대해 그대로 다시 박히므로 방어는 유지된다.
            if robot.planned_path:
                self.reservation.commit(rid, robot.planned_path, dwell=1)
            else:
                # 경로 완주 = 더 이상 갈 곳 없음 → 전부 반납.
                # commit([])은 빈 경로면 즉시 return True라 아무것도 안 지운다 → 직접 release.
                # (IDLE 콜백만으로는 부족: 작업대에서 사람을 기다리는 WAITING_FOR_PICK은
                #  IDLE이 아니라서, 그동안 걸어온 경로가 통째로 예약된 채 남는다.)
                # 정지한 로봇은 excluded_transit(현재 노드)으로 이미 hard 회피되고,
                # 회랑은 keep_indefinite로 보존되므로 안전하다.
                self.reservation.release(rid, fire_callbacks=False, keep_indefinite=True)

        # 위치 기반 회랑 자동 해제 → 깨어난 대기 AGV dispatch (Phase 4.5.4a 단일화)
        released = self.staging_manager.check_position_release(rid, node)
        self._dispatch_released_agv(released)

        # B-selfguard flush: forward 완료로 in_flight 비워졌고 위치가 fresh →
        # 이동 중 보류됐던 재계획을 *지금* 실행. 옛 plan은 폐기되므로 아래 staging/도착/
        # continue 로직(전부 옛 경로 기준)은 건너뛴다.
        if self._flush_pending_replan(rid):
            self._try_dispatch_all()
            self._try_assign_pending_tasks()
            return {"type": "marker_ack", "success": True, "action": "pending_replan_flushed"}

        # 포워딩으로 미리 해제된 로봇이 스테이징 노드 도착
        # 또는 이동 중인데 corridor가 본인 owned/FREE 상태가 되면 즉시 직진 replan (우회 회피)
        if rid in self._staged_to_ws:
            target_ws, expected_node = self._staged_to_ws[rid]
            corridor = self.staging_manager.corridors.get(target_ws)
            can_proceed = (
                node == expected_node
                or (corridor is not None
                    and self.staging_manager._owner(target_ws) in (None, rid))
            )
            if can_proceed:
                del self._staged_to_ws[rid]
                self._plan_and_publish_move(rid, node, target_ws)
                return {"type": "marker_ack", "success": True, "action": "staging_released_proceed"}

        # 스테이징 대기 중인 로봇 처리
        if self.staging_manager.is_staged_agv(node, rid):
            # gateway 도착 시 점유자가 아직 corridor area 밖이면 선점 시도
            preempted = self.staging_manager.try_preempt_at_gateway(node, rid)
            if preempted is not None:
                target_ws = preempted.target_ws
                # 선점한 본인 — 정상 진입 plan 발행
                self._plan_and_publish_move(rid, node, target_ws)
                # 이전 점유자 — 현재 위치에서 canonical staging으로 재라우팅
                prev_robot = self.robot_manager.get_robot(preempted.previous_rid)
                if prev_robot is not None:
                    print(f"[RequestHandler] Robot {preempted.previous_rid}: preempted by "
                          f"AGV-{rid} at W{target_ws}, re-routing from {prev_robot.current_node}")
                    self._plan_and_publish_move(
                        preempted.previous_rid, prev_robot.current_node, target_ws
                    )
                return {"type": "marker_ack", "success": True, "action": "corridor_preempted"}

            print(f"[RequestHandler] Robot {rid}: staging at node {node}, holding commands")
            # 수정 31: staged AGV가 직전 노드를 비웠으므로 블록된 로봇 재시도
            self._try_dispatch_all()
            return {"type": "marker_ack", "success": True, "action": "staging_wait"}

        # 스테이징 트리거 마커 체크 (퇴출 중인 로봇만 — RETURNING_SHELF 상태)
        # DELIVERING_TO_WS 상태(입장 경로)에서 trigger 노드를 지날 때는 무시
        released_by_trigger = None
        if robot.status == RobotStatus.RETURNING_SHELF:
            released_by_trigger = self.staging_manager.handle_marker_trigger(rid, node)
        # 트리거 release → 깨어난 대기 AGV dispatch (Phase 4.5.4a 단일화)
        self._dispatch_released_agv(released_by_trigger)

        # 태스크 목표 노드 도착 여부 확인
        task_id = robot.current_task_id
        if task_id:
            task = self.task_manager.get_task(task_id)
            if task:
                current_st = task.get_current_subtask()
                if current_st and node == current_st.target_node:
                    # 목표 노드 도착 → 서브태스크 처리
                    result = self._process_arrival(robot, task, current_st)
                    # 블록된 다른 로봇 재시도
                    self._try_dispatch_all()
                    self._try_assign_pending_tasks()
                    return result

        # 수정 89: 복귀 중 인터셉트를 '마커 직후(in-flight 비어있는 이 순간)'에 시도.
        # _try_assign_pending_tasks는 _send_next_command 뒤에 호출돼 항상 in-flight가 다시
        # 차서 인터셉트가 영영 skip됐다(수정 87로 태스크엔 도달하나 이 타이밍 게이트에 막힘).
        # 여기(다음 복귀 명령 전, fresh 상태)서 검사 → 안전하게 우회. 다운스트림 링크(새 태스크
        # 소비)는 기존 포워딩 도착 핸들러(FORWARD_SHELF, 수정 15)가 담당하므로 소유권 이전 불요.
        # 이미 복귀 목적지에 도착한 경우는 위 _process_arrival이 먼저 처리 → 여기 도달 안 함(자연 폴백).
        if (robot.status == RobotStatus.RETURNING_SHELF
                and self._try_intercept_for_carried_shelf(rid)):
            self._try_dispatch_all()
            self._try_assign_pending_tasks()
            return {"type": "marker_ack", "success": True, "action": "intercepted"}

        # 목표 노드가 아닌 중간 노드 → 다음 cmd 발행
        # (REFACTOR F Phase 4.1: lookahead_replan 삭제 — reservation이 plan 시점에 차단)
        self._send_next_command(rid)

        # 블록된 다른 로봇 재시도
        self._try_dispatch_all()
        self._try_assign_pending_tasks()

        return {"type": "marker_ack", "success": True, "action": "en_route"}

    # _calc_heading은 PathPlanner.calc_heading_from_path로 이동

    # ─── 명령 완료 보고 (turn/lift cmd_ack) ───

    def _handle_cmd_ack(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        AGV 명령 완료 보고 (lift_up / lift_down)

        요청:
        {"type": "cmd_ack", "rid": 1, "cmd": "lift_up", "status": "done"}
        """
        rid = data.get("rid")
        cmd = data.get("cmd")

        if rid is None or cmd is None:
            return self._error_response("Missing 'rid' or 'cmd' in cmd_ack")

        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return self._error_response(f"Robot {rid} not found")

        self._mark_online(rid)   # 수정 75: presence fallback (마커 보고와 동일)

        # REFACTOR E 3.2: turn/lift의 ACK = cmd_ack. 큐 ack가 in_flight 해제.
        # I4 일치성: in_flight cmd와 ACK cmd 일치해야 정상.
        was_in_place = False   # 수정 82: 제자리 유지(hold) lift 여부 (ack 전에 캡처)
        queue = self.command_queues.get(rid)
        if queue is not None and queue.in_flight is not None:
            in_flight = queue.in_flight
            was_in_place = in_flight.in_place
            if in_flight.cmd != cmd:
                print(f"[REFACTOR E] WARN cmd_ack mismatch: rid={rid}, "
                      f"in_flight.cmd={in_flight.cmd}, ack.cmd={cmd}")
            queue.ack()

        # 회전 완료는 태스크 유무와 무관하게 heading 갱신 + 다음 명령 전송
        # (return-home 등 태스크 없는 이동 중에도 heading을 정확히 유지해야 함)
        if cmd in ("turn_left", "turn_right", "turn_180"):
            self.robot_manager.apply_turn(robot.rid, cmd)
            # 수정 85: 회전 완료 → footprint(이웃칸 일시 점유) 해제. 대기하던 진입 로봇은
            # 바로 아래 _try_dispatch_all()에서 재시도되어 풀린다.
            self.reservation.release_turn_footprint(robot.rid)

            # B-selfguard flush: turn 완료로 heading fresh → 보류 재계획 우선 실행.
            # 보류분이 있으면 옛 큐의 다음 명령 대신 새 plan을 발행(옛 경로 폐기).
            if not self._flush_pending_replan(robot.rid):
                self._send_next_command(robot.rid)
            self._try_dispatch_all()
            return {"type": "cmd_ack_response", "success": True, "action": f"turned_{cmd}"}

        # 수정 82: 제자리 유지(hold) lift = '내려놓고 돌기'의 물리 동작만. 워크플로우 전이
        # (배달/반납 완료, 태스크 진행, 선반 status 변경) 절대 금지 — 그러면 선반을 복도에
        # 잃어버린다(규칙 3). carrying_shelf/CARRIED 그대로 유지하고 다음 명령만 발행.
        if was_in_place:
            self._send_next_command(robot.rid)
            self._try_dispatch_all()
            return {"type": "cmd_ack_response", "success": True, "action": "in_place_lift"}

        # REFACTOR E 3.4: 리프트 완료는 큐 ack가 자동 처리 (in_flight 비워짐)

        task_id = robot.current_task_id
        task = self.task_manager.get_task(task_id) if task_id else None
        if not task:
            return {"type": "cmd_ack_response", "success": True, "action": "no_task"}

        current_st = task.get_current_subtask()
        if not current_st:
            return {"type": "cmd_ack_response", "success": True, "action": "no_subtask"}

        # 약점 4: lift_up 결과 검증 — AGV가 실제로 든 선반과 서버 기대(carrying_shelf) 비교.
        # ('shelf_id' 키가 있을 때만 = 결과를 보고하는 시뮬. 실물 UART는 키 없음 → 스킵)
        # 불일치(특히 실제=None) = 빈/오 리프트 → 서버↔AGV 상태 분기. 이전엔 서버가
        # 영영 몰라 유령 선반을 계속 운반했음(분실 영구화). 이제 즉시 드러난다.
        if cmd == "lift_up" and "shelf_id" in data:
            reported = data.get("shelf_id")
            if reported != robot.carrying_shelf:
                print(f"[AGVServer] ⚠️ lift_up 결과 불일치: AGV-{rid} "
                      f"기대 선반={robot.carrying_shelf}, 실제={reported} "
                      f"→ 빈/오 리프트 감지 (선반 분실 위험)")

        if cmd == "lift_up":
            result = self._handle_pickup_ack(robot, task, current_st)
        elif cmd == "lift_down":
            result = self._handle_putdown_ack(robot, task, current_st)
        else:
            return {"type": "cmd_ack_response", "success": True, "action": "unknown_cmd"}

        # 수정 31: 리프트 중이라 보류했던 deadlock yield를 이제 재해제
        self._try_dispatch_all()
        return result

    # ─── 스테이징 마커 트리거 ───

    def handle_marker_trigger(self, rid: int, marker_id: int):
        """Point D: 마커 인식 이벤트 → 스테이징 트리거 처리

        퇴출 AGV가 트리거 노드 마커를 인식하면 대기 AGV를 해제.
        """
        released = self.staging_manager.handle_marker_trigger(rid, marker_id)
        if released:
            # 대기 AGV를 작업대로 진입시킴
            released_robot = self.robot_manager.get_robot(released.rid)
            start = released_robot.current_node if released_robot else released.staging_node
            self._plan_and_publish_move(released.rid, start, released.target_ws)
        else:
            # Bug B fix: 대기 AGV 없이 회랑 FREE → PENDING 작업 재배정 시도
            # (AT_WORKSTATION + 회랑 점유로 PENDING됐던 작업이 이제 배정 가능)
            ws_node = self.staging_manager._trigger_to_ws.get(marker_id)
            if ws_node is not None:      # 노드 0은 유효한 노드다

                corridor = self.staging_manager.corridors.get(ws_node)
                if corridor and self.staging_manager._owner(ws_node) is None:
                    self._try_assign_pending_tasks()
        return released
