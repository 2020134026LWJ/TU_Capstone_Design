"""
RequestHandler — AGV 이벤트 수신 (마커, cmd_ack, marker_trigger) Mixin

AGV → 서버 방향의 이벤트 진입점들. 위치 갱신, heading 갱신, 후속 명령 발행 트리거.

self 상태 접근:
  - self._reserved_nodes, self._yielded_staging_robots, self._staged_to_ws
  - self.robot_manager, self.shelf_manager, self.staging_manager
  - self.path_planner, self.task_manager

다른 mixin 호출:
  - MovementMixin: _plan_and_publish_move, _send_next_command, _retry_blocked_robots
  - WorkflowMixin: _try_assign_pending_tasks, _process_arrival, _handle_pickup_ack, _handle_putdown_ack
  - Base: _error_response
"""

from typing import Any, Dict, List, Optional

from .robot_manager import RobotStatus
from .staging_manager import CorridorState


class MarkerMixin:
    """AGV 이벤트 수신 핸들러"""

    # ─── 마커 인식 처리 (위치 보고 + 다음 명령 결정) ───

    def _handle_marker_report(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """AGV 마커 인식 → 위치 갱신 + 스테이징 체크 + 다음 명령 결정"""
        rid = data.get("rid")
        marker_id = data.get("marker_id")
        if rid is None or marker_id is None:
            return self._error_response("Missing 'rid' or 'marker_id'")

        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return self._error_response(f"Robot {rid} not found")

        node = int(marker_id)
        # 도착 노드 예약 해제 (이제 current_node로 갱신되므로 예약 불필요)
        if self._reserved_nodes.get(node) == rid:
            del self._reserved_nodes[node]
        # Layer 1.3: forward 완료 → in-flight 해제 (다음 cmd 발행 가능)
        self._in_flight_cmds.pop(rid, None)
        self.robot_manager.update_robot_position(rid, node)

        # heading 업데이트: 마커 메시지에 포함된 경우 우선 사용 (ArUco 포즈 기반)
        # 없으면 경로 기반 계산 (이전 방식 폴백)
        reported_heading = data.get("heading")
        if reported_heading is not None:
            robot.heading = int(reported_heading)
            if not robot.heading_initialized:
                robot.heading_initialized = True
                print(f"[RequestHandler] Robot {rid}: heading initialized to {robot.heading}° (first marker)")
                self._try_assign_pending_tasks()
        else:
            robot.heading = self.path_planner.calc_heading_from_path(
                robot.planned_path, node
            ) or robot.heading

        # planned_path slide: 이미 지나친 노드 제거 → A* 시간 예약을 실제 진행과 정합
        # (heading 계산이 prev_node를 보므로 위 fallback 뒤에서 실행)
        if node in robot.planned_path:
            idx = robot.planned_path.index(node)
            robot.planned_path = robot.planned_path[idx:]

        # 위치 기반 회랑 자동 해제
        released = self.staging_manager.check_position_release(rid, node)
        if released is not None:
            released_robot = self.robot_manager.get_robot(released.rid)
            if released_robot and released.rid in self._yielded_staging_robots:
                # deadlock yield로 staging_node 떠난 상태 → 현재 위치(yield_node)에서 직접 plan
                self._yielded_staging_robots.discard(released.rid)
                print(f"[RequestHandler] Robot {released.rid}: yielded-staging released "
                      f"(at {released_robot.current_node}) → {released.target_ws}")
                self._plan_and_publish_move(
                    released.rid, released_robot.current_node, released.target_ws
                )
            # staged robot이 아직 staging_node에 미도착이면 → 도착 후 처리 (desync 방지)
            elif released_robot and released_robot.current_node != released.staging_node:
                self._staged_to_ws[released.rid] = (released.target_ws, released.staging_node)
                print(f"[RequestHandler] Robot {released.rid}: released early (at {released_robot.current_node}), "
                      f"waiting for staging_node {released.staging_node}")
            else:
                start = released_robot.current_node if released_robot else released.staging_node
                self._plan_and_publish_move(released.rid, start, released.target_ws)

        # 포워딩으로 미리 해제된 로봇이 스테이징 노드 도착
        # 또는 이동 중인데 corridor가 본인 owned/FREE 상태가 되면 즉시 직진 replan (우회 회피)
        if rid in self._staged_to_ws:
            target_ws, expected_node = self._staged_to_ws[rid]
            corridor = self.staging_manager.corridors.get(target_ws)
            can_proceed = (
                node == expected_node
                or (corridor is not None
                    and (corridor.state == CorridorState.FREE
                         or corridor.occupying_rid == rid))
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
            self._retry_blocked_robots()
            return {"type": "marker_ack", "success": True, "action": "staging_wait"}

        # 스테이징 트리거 마커 체크 (퇴출 중인 로봇만 — RETURNING_SHELF 상태)
        # DELIVERING_TO_WS 상태(입장 경로)에서 trigger 노드를 지날 때는 무시
        released_by_trigger = None
        if robot.status == RobotStatus.RETURNING_SHELF:
            released_by_trigger = self.staging_manager.handle_marker_trigger(rid, node)
        if released_by_trigger:
            released_robot = self.robot_manager.get_robot(released_by_trigger.rid)
            if released_robot and released_by_trigger.rid in self._yielded_staging_robots:
                # deadlock yield로 staging_node 떠난 상태 → 현재 위치(yield_node)에서 직접 plan
                self._yielded_staging_robots.discard(released_by_trigger.rid)
                print(f"[RequestHandler] Robot {released_by_trigger.rid}: yielded-staging trigger-released "
                      f"(at {released_robot.current_node}) → {released_by_trigger.target_ws}")
                self._plan_and_publish_move(
                    released_by_trigger.rid, released_robot.current_node, released_by_trigger.target_ws
                )
            # staged robot이 아직 staging_node에 미도착이면 → 도착 후 처리 (desync 방지)
            elif released_robot and released_robot.current_node != released_by_trigger.staging_node:
                self._staged_to_ws[released_by_trigger.rid] = (released_by_trigger.target_ws, released_by_trigger.staging_node)
                print(f"[RequestHandler] Robot {released_by_trigger.rid}: trigger-released early "
                      f"(at {released_robot.current_node}), waiting for staging_node {released_by_trigger.staging_node}")
            else:
                start = released_robot.current_node if released_robot else released_by_trigger.staging_node
                self._plan_and_publish_move(released_by_trigger.rid, start, released_by_trigger.target_ws)

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
                    self._retry_blocked_robots()
                    self._try_assign_pending_tasks()
                    return result

        # 목표 노드가 아닌 중간 노드 → lookahead 검사 → 충돌 예정이면 사전 replan
        # replan되면 _plan_and_publish_move가 이미 첫 cmd 발행했으므로 _send_next_command 스킵
        if not self._lookahead_replan(rid):
            self._send_next_command(rid)

        # 블록된 다른 로봇 재시도
        self._retry_blocked_robots()
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

        # Layer 1.3: turn/lift 완료 → in-flight 해제 (다음 cmd 발행 가능)
        self._in_flight_cmds.pop(rid, None)

        # 회전 완료는 태스크 유무와 무관하게 heading 갱신 + 다음 명령 전송
        # (return-home 등 태스크 없는 이동 중에도 heading을 정확히 유지해야 함)
        if cmd in ("turn_left", "turn_right", "turn_180"):
            self.robot_manager.apply_turn(robot.rid, cmd)
            self._send_next_command(robot.rid)
            self._retry_blocked_robots()
            return {"type": "cmd_ack_response", "success": True, "action": f"turned_{cmd}"}

        # 수정 31: 리프트 완료 보고 → _lifting_robots 해제 (task 유무와 무관하게)
        if cmd in ("lift_up", "lift_down"):
            self._lifting_robots.discard(rid)

        task_id = robot.current_task_id
        task = self.task_manager.get_task(task_id) if task_id else None
        if not task:
            return {"type": "cmd_ack_response", "success": True, "action": "no_task"}

        current_st = task.get_current_subtask()
        if not current_st:
            return {"type": "cmd_ack_response", "success": True, "action": "no_subtask"}

        if cmd == "lift_up":
            result = self._handle_pickup_ack(robot, task, current_st)
        elif cmd == "lift_down":
            result = self._handle_putdown_ack(robot, task, current_st)
        else:
            return {"type": "cmd_ack_response", "success": True, "action": "unknown_cmd"}

        # 수정 31: 리프트 중이라 보류했던 deadlock yield를 이제 재해제
        self._retry_blocked_robots()
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
            if ws_node:
                corridor = self.staging_manager.corridors.get(ws_node)
                if corridor and corridor.state == CorridorState.FREE:
                    self._try_assign_pending_tasks()
        return released
