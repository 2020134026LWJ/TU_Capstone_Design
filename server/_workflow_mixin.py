"""
RequestHandler — 주문/태스크 워크플로우 Mixin (구 TaskMixin)

GUI/CLI → 서버 방향의 워크플로우 처리. 주문 시작, 배정, 도착 후 다음 단계,
포워딩, 인터셉트, F-노드 분기 등 비즈니스 로직.

self 상태 접근:
  - self.task_manager, robot_manager, shelf_manager, staging_manager
  - self.path_planner, self.task_scheduler, self.mqtt_publisher
  - self._forwarded_shelf_handlers
  - self.DEMO_MODE

다른 mixin 호출:
  - MovementMixin: _plan_and_publish_move, _clear_robot_reservation,
                    _replan_for_placed_shelf, _get_idle_wait_node
  - Base: _error_response
"""

import json
from typing import Any, Dict, List, Optional, Set

from .robot_manager import RobotStatus
from .shelf_manager import ShelfStatus
from .staging_manager import CorridorState
from .task_manager import SubTaskType, TaskStatus


class WorkflowMixin:
    """주문/태스크/서브태스크 흐름"""

    # ─── 주문 시작 ───

    def _handle_start_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        주문 시작 (엑셀 DB에서 로드 → OrderOptimizer 최적화 → 작업 등록)

        요청:
        {
            "type": "start_order",
            "사용자ID": 1,
            "주문번호": 1
        }
        """
        user_id = data.get("사용자ID")
        order_id = data.get("주문번호", 1)

        if user_id is None:
            return self._error_response("Missing '사용자ID'")

        # stock 검증은 GUI 서버(warehouse_server_v2)가 단독 수행. AGV는 DB 미접근.
        # → AGV 자체 검증으로 인한 self-contradiction(수정 47) 영구 제거.

        schedule = self.task_scheduler.schedule_order(user_id=user_id, order_id=order_id)
        if not schedule:
            return self._error_response(f"Order not found: user={user_id}, order={order_id}")

        workstation_id = schedule["workstation"]
        group_id = f"T{user_id}_{order_id}"

        # 선반 1개 단위로 태스크 분리 → 여러 로봇이 동시에 각 선반 처리 가능
        created_tasks = []
        for idx, shelf_task in enumerate(schedule["tasks"]):
            sub_task_id = f"{group_id}_{idx}"
            task = self.task_manager.create_task(
                task_id=sub_task_id,
                workstation_id=workstation_id,
                items=shelf_task.items,
                optimized_shelf_sequence=[shelf_task.shelf_node],
            )
            if task:
                created_tasks.append((sub_task_id, shelf_task))

        if not created_tasks:
            return self._error_response("Failed to create tasks")

        self._try_assign_pending_tasks()

        shelves = []
        for idx, (sub_task_id, shelf_task) in enumerate(created_tasks):
            task_obj = self.task_manager.get_task(sub_task_id)
            items = []
            if task_obj:
                for st in task_obj.subtasks:
                    if st.subtask_type == SubTaskType.WAIT_PICKING:
                        items = st.items_to_pick
                        break
            shelves.append({
                "order": idx + 1,
                "shelf_label": shelf_task.shelf_label,
                "shelf_node": shelf_task.shelf_node,
                "items": items,
            })

        return {
            "type": "start_order_response",
            "success": True,
            "task_id": group_id,
            "user_id": user_id,
            "workstation_id": workstation_id,
            "shelves": shelves,
            "total_shelves": len(shelves),
            "total_items": schedule["total_items"],
        }

    # ─── 배치 작업 등록 ───

    def _handle_batch_task(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        작업 일괄 등록

        요청:
        {
            "type": "batch_task_request",
            "tasks": [
                {"task_id": "T1", "workstation_id": 33, "items": ["A","B","Z","D"]},
                {"task_id": "T2", "workstation_id": 34, "items": ["C","X","U","I"]}
            ]
        }
        """
        task_list = data.get("tasks")
        if not task_list:
            return self._error_response("Missing 'tasks' field")

        created_tasks = self.task_manager.create_batch_tasks(task_list)

        # 대기 중인 작업에 유휴 로봇 배정 시도
        assignments = self._try_assign_pending_tasks()

        return {
            "type": "batch_task_response",
            "success": True,
            "tasks_created": len(created_tasks),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "shelves_needed": t.shelf_sequence,
                    "status": t.status.value,
                    "assigned_robot": t.assigned_robot,
                }
                for t in created_tasks
            ],
            "assignments": assignments,
        }

    # ─── 작업 배정 (공정/F-노드/인터셉트) ───

    def _count_active_robots_per_ws(self) -> Dict[int, int]:
        """WS별 현재 비유휴 로봇 수 계산 (공정 배정용)"""
        counts: Dict[int, int] = {}
        for robot in self.robot_manager.get_all_robots():
            if robot.status == RobotStatus.IDLE:
                continue
            if robot.current_task_id:
                task = self.task_manager.get_task(robot.current_task_id)
                if task:
                    ws = task.workstation_id
                    counts[ws] = counts.get(ws, 0) + 1
        return counts

    def _try_assign_pending_tasks(self) -> List[Dict]:
        """대기 중인 작업에 유휴 로봇 할당 시도 (공정 배정: WS별 활성 로봇 수 기반)"""
        assignments = []
        rotation_counts: Dict[str, int] = {}  # Bug A: 선반 순서 변경 시도 횟수
        blocked_task_ids: Set[str] = set()     # 이번 호출에서 블록된 태스크 (재시도 방지)

        while True:
            # 매 iteration마다 WS별 활성 로봇 수 재계산 (직전 배정 반영)
            active_per_ws = self._count_active_robots_per_ws()
            task = self.task_manager.get_next_pending_task_fair(
                active_per_ws, exclude=blocked_task_ids
            )
            if not task:
                break

            # 첫 번째 선반의 위치를 기준으로 가장 가까운 로봇 배정
            # [DEMO MODE] home_node가 작업대와 일치하는 로봇만 허용
            first_shelf = task.shelf_sequence[0] if task.shelf_sequence else None
            demo_dedicated_rid = None
            if self.DEMO_MODE:
                # AGV의 home_node가 task의 workstation_id와 같은 로봇만 배정
                for r in self.robot_manager.get_all_robots():
                    if r.home_node == task.workstation_id:
                        demo_dedicated_rid = r.rid
                        break
            robot = self.robot_manager.get_available_robot(
                target_node=first_shelf,
                path_planner=self.path_planner,
                dedicated_rid=demo_dedicated_rid,  # [DEMO MODE] None이면 무시
            )
            if not robot:
                if self._try_intercept_returning_shelf(task):
                    continue  # 인터셉트 성공 → 다음 대기 작업 처리 시도
                break         # 유휴 로봇 없음 → 종료

            # 작업 시작
            first_st = self.task_manager.start_task(task.task_id, robot.rid)
            if not first_st:
                break

            # F 노드: 선반 현재 위치 체크 (6분기, Bug A 포함)
            # first_st.shelf_id 사용 (shelf_sequence[0]은 완료된 선반이 남아있어 stale할 수 있음)
            actual_first_shelf = first_st.shelf_id if first_st else first_shelf
            shelf_obj = self.shelf_manager.get_shelf(actual_first_shelf) if actual_first_shelf else None
            if shelf_obj:
                avail = self._get_shelf_availability(actual_first_shelf, robot.rid)
                if avail == "direct":
                    # AT_WORKSTATION + 사용 가능 → 작업대로 직행
                    first_st.target_node = shelf_obj.current_node
                    print(f"[RequestHandler] F-node: shelf {actual_first_shelf} at WS {shelf_obj.current_node}, "
                          f"task {task.task_id} → direct to WS")
                elif avail == "pending":
                    # Bug A: 다음 선반이 있으면 순서 변경 후 재시도 (단일 선반 태스크는 즉시 pass)
                    rotations = rotation_counts.get(task.task_id, 0)
                    max_rotations = len(task.shelf_sequence) - 1
                    if rotations < max_rotations:
                        next_shelf_id = self.task_manager.rotate_shelf_to_end(task.task_id)
                        if next_shelf_id is not None:
                            rotation_counts[task.task_id] = rotations + 1
                            task.status = TaskStatus.PENDING
                            task.assigned_robot = None
                            first_st.status = TaskStatus.PENDING
                            print(f"[RequestHandler] F-node: shelf {actual_first_shelf} blocked, "
                                  f"rotating to shelf {next_shelf_id} (task {task.task_id}, "
                                  f"attempt {rotations + 1}/{max_rotations})")
                            continue  # 같은 task, 새 first_shelf로 재시도
                    # 선반 차단 → 이번 호출에서 이 태스크 건너뜀, 다른 태스크 시도
                    rotation_counts.pop(task.task_id, None)
                    task.status = TaskStatus.PENDING
                    task.assigned_robot = None
                    first_st.status = TaskStatus.PENDING
                    print(f"[RequestHandler] F-node: shelf {actual_first_shelf} unavailable "
                          f"(status={shelf_obj.status.value}), task {task.task_id} → skip")
                    blocked_task_ids.add(task.task_id)
                    continue  # 다른 태스크 시도 (break 대신 continue)
                # else "go": IN_PLACE + 예약 없음 → 그대로 진행
            rotation_counts.pop(task.task_id, None)  # 성공적 배정 시 카운터 초기화

            # 로봇 상태 업데이트
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
            robot.current_task_id = task.task_id

            # 경로 계획: 로봇 현재 위치 → 선반
            move_result = self._plan_and_publish_move(
                robot.rid, robot.current_node, first_st.target_node
            )

            assignments.append({
                "task_id": task.task_id,
                "robot_id": robot.rid,
                "first_target": first_st.target_node,
                "path_planned": move_result is not None,
            })

        return assignments

    def _try_intercept_returning_shelf(self, task) -> bool:
        """
        Node U: 이동 중 새 주문 인터셉트 (같은 선반)

        대기 중인 task의 첫 선반을 RETURNING_SHELF 상태로 운반 중인 로봇이
        있으면, RETURN_SHELF 서브태스크를 FORWARD_SHELF로 바꾸고 새 작업대로
        경로를 재계획한다.

        Returns:
            True: 인터셉트 성공
            False: 인터셉트 불가

        in-flight cmd 가드: ACK 도착 전 robot 상태(carrying_shelf, current_node,
        heading)가 stale → intercept가 stale 상태 기준으로 재계획 시 off-by-one
        cascade 발생. carrying robot의 큐 in_flight 비었을 때만 발화.
        """
        first_shelf = task.shelf_sequence[0] if task.shelf_sequence else None
        if first_shelf is None:
            return False

        carrying_robot = self.robot_manager.get_robot_carrying_shelf(first_shelf)
        if not carrying_robot:
            return False

        if carrying_robot.status != RobotStatus.RETURNING_SHELF:
            return False

        # ─── REFACTOR E 3.2: ANY 명령 in-flight면 intercept 금지 (수정 46.1 계승) ───
        # 서버의 robot 상태(current_node, heading, carrying_shelf)가 AGV 실제 상태와
        # 정합인 순간(ACK/마커 직후)에만 intercept를 발화시켜야 stale 재계획 방지.
        carrying_queue = self.command_queues.get(carrying_robot.rid)
        if carrying_queue is not None and carrying_queue.in_flight is not None:
            in_flight_cmd = carrying_queue.in_flight.cmd
            print(f"[RequestHandler] Node U: intercept skipped (Robot {carrying_robot.rid} "
                  f"has '{in_flight_cmd}' in-flight) — task {task.task_id} stays PENDING")
            return False

        task_id = carrying_robot.current_task_id
        if not task_id:
            return False

        carrying_task = self.task_manager.get_task(task_id)
        if not carrying_task:
            return False

        current_st = carrying_task.get_current_subtask()
        if not current_st or current_st.subtask_type != SubTaskType.RETURN_SHELF:
            return False

        # 인터셉트: RETURN_SHELF → FORWARD_SHELF, 목적지를 새 작업대로 변경
        target_ws = task.workstation_id
        current_st.subtask_type = SubTaskType.FORWARD_SHELF
        current_st.target_node = target_ws

        self.robot_manager.set_robot_status(carrying_robot.rid, RobotStatus.DELIVERING_TO_WS)

        # 인터셉트는 트리거 노드를 통과하지 못하므로 회랑을 직접 해제
        # (release_corridor_without_trigger가 is_exiting=False 리셋과 큐 승계를 일관 처리)
        for ws_node, corridor in self.staging_manager.corridors.items():
            if corridor.occupying_rid == carrying_robot.rid:
                released = self.staging_manager.release_corridor_without_trigger(
                    ws_node, carrying_robot.rid
                )
                if released:
                    released_robot = self.robot_manager.get_robot(released.rid)
                    if released_robot:
                        self._plan_and_publish_move(
                            released_robot.rid, released_robot.current_node, ws_node
                        )
                break

        # 피드백 3: MQTT 재발행으로 복귀 중인 로봇에게 새 경로 전달
        # (로봇은 새 plan 메시지를 수신하면 현재 이동을 중단하고 새 목적지로 향함)
        self._plan_and_publish_move(carrying_robot.rid, carrying_robot.current_node, target_ws)

        print(f"[RequestHandler] Node U: Robot {carrying_robot.rid} intercepted while returning "
              f"shelf {first_shelf} → redirecting to WS {target_ws} for task {task.task_id}")
        return True

    # ─── 도착 / lift_up / lift_down 처리 ───

    def _process_arrival(self, robot, task, current_st) -> Dict[str, Any]:
        """로봇 도착 후 서브태스크 유형에 따라 처리"""

        st_type = current_st.subtask_type

        if st_type == SubTaskType.GO_TO_SHELF:
            # 선반에 도착 → lift_up 명령 발행 (cmd_ack 대기)
            result = self.task_manager.handle_subtask_complete(task.task_id)
            next_st = task.get_current_subtask()

            if next_st and next_st.subtask_type == SubTaskType.PICKUP_SHELF:
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.PICKING_UP_SHELF)
                self.shelf_manager.mark_shelf_picked_up(next_st.shelf_id, robot.rid)
                self.robot_manager.set_carrying_shelf(robot.rid, next_st.shelf_id)

                # REFACTOR E 3.4: lift_up도 큐를 거쳐 발행 (I1 단일 발행점).
                # robot.command_queue에 push 후 _send_next_command가 큐 dispatch + publish.
                robot.command_queue.append("lift_up")
                self._send_next_command(robot.rid)
                return {
                    "type": "robot_arrived_ack",
                    "success": True,
                    "action": "waiting_shelf_pickup",
                    "shelf_id": next_st.shelf_id,
                }

        elif st_type == SubTaskType.DELIVER_TO_WS:
            # 작업대에 도착 → 픽업 대기
            self.shelf_manager.mark_shelf_at_workstation(
                current_st.shelf_id, current_st.target_node
            )
            result = self.task_manager.handle_subtask_complete(task.task_id)
            next_st = task.get_current_subtask()

            if next_st and next_st.subtask_type == SubTaskType.WAIT_PICKING:
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.WAITING_FOR_PICK)
                # 선반이 CARRIED → AT_WORKSTATION 전환 → PENDING 태스크 재배정 시도
                self._try_assign_pending_tasks()
                # GUI에 AGV 도착 알림 (warehouse/shelf/arrived)
                user_id = int(task.task_id.split("_")[0][1:])
                shelf_label = self.shelf_manager.shelves[next_st.shelf_id].label
                self.mqtt_publisher.client.publish(
                    "warehouse/shelf/arrived",
                    json.dumps({"사용자ID": user_id, "선반번호": shelf_label}, ensure_ascii=False)
                )
                return {
                    "type": "robot_arrived_ack",
                    "success": True,
                    "action": "wait_picking",
                    "shelf_id": next_st.shelf_id,
                    "items_to_pick": next_st.items_to_pick,
                }

        elif st_type in (SubTaskType.RETURN_SHELF, SubTaskType.FORWARD_SHELF):
            # 선반 복귀/포워딩 목적지 도착 → lift_down 명령 발행 (cmd_ack 대기)
            # REFACTOR E 3.4: 큐 경유 (I1 단일 발행점)
            shelf_id = current_st.shelf_id
            robot.command_queue.append("lift_down")
            self._send_next_command(robot.rid)
            action = "waiting_shelf_putdown" if st_type == SubTaskType.RETURN_SHELF else "waiting_shelf_putdown_forward"
            return {
                "type": "robot_arrived_ack",
                "success": True,
                "action": action,
                "shelf_id": shelf_id,
            }

        return {"type": "robot_arrived_ack", "success": True, "action": "unknown_state"}

    def _handle_pickup_ack(self, robot, task, current_st) -> Dict[str, Any]:
        """pickup ack → PICKUP_SHELF 완료 → DELIVER_TO_WS 시작"""
        if current_st.subtask_type != SubTaskType.PICKUP_SHELF:
            print(f"[RequestHandler] Warning: pickup ack but current subtask is {current_st.subtask_type}")
            return {"type": "cmd_ack_response", "success": False, "error": "not_picking_up"}

        # PICKUP_SHELF 완료 → DELIVER_TO_WS
        result = self.task_manager.handle_subtask_complete(task.task_id)
        next_st = task.get_current_subtask()

        if next_st and next_st.subtask_type == SubTaskType.DELIVER_TO_WS:
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.DELIVERING_TO_WS)
            self._plan_and_publish_move(
                robot.rid, robot.current_node, next_st.target_node
            )
            return {
                "type": "cmd_ack_response",
                "success": True,
                "action": "delivering_to_ws",
                "shelf_id": current_st.shelf_id,
                "target_node": next_st.target_node,
            }

        elif next_st and next_st.subtask_type == SubTaskType.RETURN_SHELF:
            # 포워딩된 선반 재픽업 완료 → 원래 위치(home)로 반납
            shelf_id = current_st.shelf_id
            home_node = self.shelf_manager.get_shelf_home(shelf_id)
            return_node = home_node or shelf_id
            if home_node and not self.shelf_manager.is_position_available(home_node):
                alt = self.shelf_manager.find_nearest_empty_position(
                    robot.current_node, self.path_planner
                )
                if alt:
                    return_node = alt
            next_st.target_node = return_node
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.RETURNING_SHELF)
            self.staging_manager.mark_exiting(robot.current_node, robot.rid)
            self._plan_and_publish_move(robot.rid, robot.current_node, return_node)
            return {
                "type": "cmd_ack_response",
                "success": True,
                "action": "returning_forwarded_shelf",
                "shelf_id": shelf_id,
                "return_to": return_node,
            }

        return {"type": "cmd_ack_response", "success": True, "action": "pickup_done"}

    def _handle_putdown_ack(self, robot, task, current_st) -> Dict[str, Any]:
        """putdown ack → RETURN_SHELF 또는 FORWARD_SHELF 완료 처리"""
        st_type = current_st.subtask_type
        shelf_id = current_st.shelf_id

        if st_type == SubTaskType.RETURN_SHELF:
            # 선반 반납 완료
            placed_node = current_st.target_node
            self.shelf_manager.mark_shelf_returned(shelf_id, placed_node)
            self.robot_manager.set_carrying_shelf(robot.rid, None)
            # 방금 배치된 노드를 경유 예정인 운반 로봇 재계획
            self._replan_for_placed_shelf(placed_node)

            result = self.task_manager.handle_subtask_complete(task.task_id)

            if result.get("action") == "task_complete":
                self._clear_robot_reservation(robot.rid)
                # 수정 46: task 완료 시 stale staging 큐 엔트리 제거
                self.staging_manager.remove_robot_from_queues(robot.rid)
                self.robot_manager.complete_task(robot.rid)
                self._try_assign_pending_tasks()
                # 새 작업이 없으면 홈 staging 노드에서 대기
                idle_wait = self._get_idle_wait_node(robot.rid)
                if robot.status == RobotStatus.IDLE and robot.current_node != idle_wait:
                    self._plan_and_publish_move(robot.rid, robot.current_node, idle_wait)
                return {
                    "type": "cmd_ack_response",
                    "success": True,
                    "action": "task_complete",
                    "task_id": task.task_id,
                }
            elif result.get("action") == "next_subtask":
                next_st = task.get_current_subtask()
                if next_st:
                    # F 노드: 다음 선반 현재 위치 체크
                    pending_resp = self._handle_fnode_next_shelf(task, robot, next_st, "return")
                    if pending_resp:
                        return pending_resp
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
                    self._plan_and_publish_move(
                        robot.rid, robot.current_node, next_st.target_node
                    )
                    return {
                        "type": "cmd_ack_response",
                        "success": True,
                        "action": "moving_to_next_shelf",
                        "target_node": next_st.target_node,
                    }

        elif st_type == SubTaskType.FORWARD_SHELF:
            # ── 수정 15: 포워딩 완료 → T1이 목적지 WS에서 WAIT_PICKING → re-pickup → RETURN ──
            dest_ws = current_st.target_node
            self.shelf_manager.mark_shelf_at_workstation(shelf_id, dest_ws)
            self.robot_manager.set_carrying_shelf(robot.rid, None)

            # 목적지 WS에서 필요한 아이템 수집 (demand 제거 전 조회)
            t2_items = self.task_manager.get_demand_items_for_ws(shelf_id, dest_ws)

            if t2_items:
                # T1 서브태스크에 WAIT_PICKING + PICKUP_SHELF + RETURN_SHELF 삽입
                self.task_manager.insert_forward_return_subtasks(
                    task.task_id, shelf_id, dest_ws, t2_items
                )

                # T2 서브태스크 스킵 및 수요 제거 (T1이 대신 처리)
                forwarded_task_id = self.task_manager.handle_shelf_forwarded(shelf_id, dest_ws)
                if forwarded_task_id:
                    new_t2_st = self.task_manager.skip_shelf_subtasks_for_forwarding(
                        forwarded_task_id, shelf_id
                    )
                    self.task_manager.remove_shelf_demand_for_shelf(forwarded_task_id, shelf_id)
                    self._reroute_robot_after_skip(forwarded_task_id, new_t2_st)
                    print(f"[RequestHandler] Shelf {shelf_id} forwarded to WS {dest_ws}: "
                          f"T1(task {task.task_id}) handles full cycle, "
                          f"T2(task {forwarded_task_id}) shelf subtasks skipped")

                # T1을 포워딩 선반 핸들러로 등록
                self._forwarded_shelf_handlers[shelf_id] = robot.rid
            else:
                # t2_items 없음 (비정상이지만 방어 처리)
                forwarded_task_id = self.task_manager.handle_shelf_forwarded(shelf_id, dest_ws)
                if forwarded_task_id:
                    print(f"[RequestHandler] Shelf {shelf_id} forwarded but no items needed "
                          f"at WS {dest_ws} (task {forwarded_task_id})")

            # 현재 서브태스크 완료 → 다음 서브태스크로 진행
            result = self.task_manager.handle_subtask_complete(task.task_id)

            if result.get("action") == "task_complete":
                self._clear_robot_reservation(robot.rid)
                # 수정 46: task 완료 시 stale staging 큐 엔트리 제거
                self.staging_manager.remove_robot_from_queues(robot.rid)
                self.robot_manager.complete_task(robot.rid)
                self._try_assign_pending_tasks()
                idle_wait = self._get_idle_wait_node(robot.rid)
                if robot.status == RobotStatus.IDLE and robot.current_node != idle_wait:
                    self._plan_and_publish_move(robot.rid, robot.current_node, idle_wait)
                return {
                    "type": "cmd_ack_response",
                    "success": True,
                    "action": "task_complete",
                    "task_id": task.task_id,
                }
            elif result.get("action") == "next_subtask":
                next_st = task.get_current_subtask()
                if next_st and next_st.subtask_type == SubTaskType.WAIT_PICKING:
                    # ★ 포워딩 후 목적지 WS에서 T2 아이템 픽업 대기
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.WAITING_FOR_PICK)
                    # AT_WORKSTATION 전환 후 PENDING 태스크 재배정 시도
                    self._try_assign_pending_tasks()
                    return {
                        "type": "cmd_ack_response",
                        "success": True,
                        "action": "wait_picking_at_forward_ws",
                        "shelf_id": shelf_id,
                        "items_to_pick": next_st.items_to_pick,
                    }
                elif next_st and next_st.subtask_type == SubTaskType.GO_TO_SHELF:
                    # F 노드: 다음 선반 현재 위치 체크
                    pending_resp = self._handle_fnode_next_shelf(task, robot, next_st, "forward")
                    if pending_resp:
                        return pending_resp
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
                    self._plan_and_publish_move(robot.rid, robot.current_node, next_st.target_node)
                    self._try_assign_pending_tasks()
                    return {
                        "type": "cmd_ack_response",
                        "success": True,
                        "action": "moving_to_next_shelf",
                        "target_node": next_st.target_node,
                    }
        else:
            print(f"[RequestHandler] Warning: putdown ack but current subtask is {st_type}")
            return {"type": "cmd_ack_response", "success": False, "error": "not_returning"}

        return {"type": "cmd_ack_response", "success": True, "action": "putdown_done"}

    # ─── shelf_complete / order_complete ───

    def _handle_shelf_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        작업자가 WS 선반 픽업 완료 신호 → AGV 반납/포워딩 (GUI → 서버)

        요청: {"type": "shelf_complete", "사용자ID": 1}
        서버가 해당 사용자 WS의 AT_WORKSTATION 선반을 자동 탐색
        """
        user_id = data.get("사용자ID")
        if user_id is None:
            return self._error_response("Missing '사용자ID'")

        # user_id → WS 노드: shelf_config의 user_id 필드 기준
        # (robot.home_node와 무관 — 교착 회피로 AGV home이 WS와 스왑돼 있을 수 있음)
        ws_node = next(
            (node for node, info in self.shelf_manager.workstations.items()
             if info.get("user_id") == user_id),
            None,
        )
        if ws_node is None:
            return self._error_response(f"Unknown user {user_id}")

        # 해당 WS에 AT_WORKSTATION인 선반 자동 탐색
        shelf_id = self.shelf_manager.get_shelf_at_ws(ws_node)
        if shelf_id is None:
            return self._error_response(f"No shelf at workstation {ws_node} (user {user_id})")

        # 해당 선반을 WAIT_PICKING 중인 task 탐색
        # (포워딩 케이스: user_id의 task가 아닌 포워딩 로봇의 task일 수 있으므로 선반 기준 탐색)
        task = self.task_manager.find_task_waiting_for_shelf(shelf_id)
        if not task:
            return self._error_response(f"No task in WAIT_PICKING for shelf {shelf_id}")

        result = self.task_manager.handle_shelf_complete(task.task_id)

        if result.get("action") == "shelf_done":
            shelf_id_r = result.get("shelf_id")
            next_action = result.get("next_action")
            robot = self.robot_manager.get_robot_carrying_shelf(shelf_id_r) if shelf_id_r else None

            if robot and next_action == "return":
                return_to = result.get("return_to", shelf_id_r)
                self.shelf_manager.mark_shelf_picked_up(shelf_id_r, robot.rid)
                # Point C: AGV 작업대 퇴출 → 회랑 exiting 표시
                self.staging_manager.mark_exiting(robot.current_node, robot.rid)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.RETURNING_SHELF)
                self._plan_and_publish_move(robot.rid, robot.current_node, return_to)
                return {"success": True, "action": "returning_shelf", "return_to": return_to}

            elif robot and next_action == "forward":
                forward_ws = result.get("forward_to_ws")
                source_ws = robot.current_node
                self.shelf_manager.mark_shelf_picked_up(shelf_id_r, robot.rid)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.DELIVERING_TO_WS)

                # Point C (포워딩): RETURN_SHELF와 동일하게 mark_exiting → 위치 기반 해제
                # 즉시 release_corridor_without_trigger 하면 gateway를 아직 못 빠져나간
                # 포워딩 로봇과 스테이징 해제 로봇이 gateway에서 충돌함
                self.staging_manager.mark_exiting(source_ws, robot.rid)
                # is_forwarding=True → 목적지 corridor busy 시 staging_node 대신 gateway에서 대기
                self._plan_and_publish_move(robot.rid, source_ws, forward_ws, is_forwarding=True)
                return {"success": True, "action": "forwarding_shelf", "forward_to_ws": forward_ws}

        elif result.get("action") == "shelf_done_pickup_for_return":
            # 포워딩된 선반 픽업 완료 → 포워딩 로봇이 re-pickup 후 반납
            shelf_id_r = result.get("shelf_id")
            robot = self.robot_manager.get_robot(task.assigned_robot) if task else None

            if robot and shelf_id_r:
                self.shelf_manager.mark_shelf_picked_up(shelf_id_r, robot.rid)
                self.robot_manager.set_carrying_shelf(robot.rid, shelf_id_r)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.PICKING_UP_SHELF)
                # REFACTOR E 3.4: 큐 경유
                robot.command_queue.append("lift_up")
                self._send_next_command(robot.rid)
                self._forwarded_shelf_handlers.pop(shelf_id_r, None)
                print(f"[RequestHandler] robot {robot.rid}: re-pickup shelf {shelf_id_r} for return")
                return {"success": True, "action": "pickup_for_return", "shelf_id": shelf_id_r}

        return self._error_response(result.get("message", "Unknown error"))

    def _handle_order_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        주문 완료 처리 (GUI → 서버)

        요청:
        {"type": "order_complete", "사용자ID": 1, "주문번호": 1}
        """
        user_id = data.get("사용자ID")
        order_id = data.get("주문번호", 1)

        if user_id is None:
            return self._error_response("Missing '사용자ID'")

        group_id = f"T{user_id}_{order_id}"
        order_tasks = [
            t for t in self.task_manager.tasks.values()
            if t.task_id.startswith(f"{group_id}_")
        ]
        if not order_tasks:
            return self._error_response(f"No tasks found for order: {group_id}")

        statuses = [t.status.value for t in order_tasks]
        print(f"[RequestHandler] order_complete: user={user_id}, group={group_id}, "
              f"task_statuses={statuses}")
        return {"success": True, "action": "order_complete", "task_id": group_id}

    # ─── F 노드 / 선반 가용성 / 유틸 ───

    def _is_shelf_targeted_by_moving_robot(self, shelf_id: int, exclude_rid: int) -> bool:
        """다른 로봇이 이미 이 선반(GO_TO_SHELF)으로 이동 중인지 확인"""
        for robot in self.robot_manager.robots.values():
            if robot.rid == exclude_rid:
                continue
            if robot.status != RobotStatus.MOVING_TO_SHELF:
                continue
            if not robot.current_task_id:
                continue
            other_task = self.task_manager.get_task(robot.current_task_id)
            if not other_task:
                continue
            current_st = other_task.get_current_subtask()
            if (current_st and
                    current_st.subtask_type == SubTaskType.GO_TO_SHELF and
                    current_st.shelf_id == shelf_id):
                return True
        return False

    def _handle_fnode_next_shelf(self, task, robot, next_st, context: str) -> Optional[Dict[str, Any]]:
        """F 노드: 다음 선반 위치 체크 (RETURN/FORWARD 공용)

        Returns:
            dict: pending 상태 → 즉시 반환할 응답
            None: go/direct → 호출자가 MOVING_TO_SHELF 처리
        """
        next_shelf_obj = self.shelf_manager.get_shelf(next_st.shelf_id) if next_st.shelf_id else None
        if not next_shelf_obj:
            return None

        avail = self._get_shelf_availability(next_st.shelf_id, robot.rid)
        if avail == "direct":
            next_st.target_node = next_shelf_obj.current_node
            print(f"[RequestHandler] F-node({context}): shelf {next_st.shelf_id} at WS "
                  f"{next_shelf_obj.current_node}, task {task.task_id} → direct to WS")
        elif avail == "pending":
            task.status = TaskStatus.PENDING
            task.assigned_robot = None
            next_st.status = TaskStatus.PENDING
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.IDLE)
            robot.current_task_id = None
            # 수정 46: IDLE 전환 시 stale staging 큐 엔트리 제거
            self.staging_manager.remove_robot_from_queues(robot.rid)
            print(f"[RequestHandler] F-node({context}): shelf {next_st.shelf_id} unavailable "
                  f"(status={next_shelf_obj.status.value}), "
                  f"task {task.task_id} → PENDING, robot {robot.rid} → IDLE")
            self._try_assign_pending_tasks()
            idle_wait = self._get_idle_wait_node(robot.rid)
            if robot.status == RobotStatus.IDLE and robot.current_node != idle_wait:
                self._plan_and_publish_move(robot.rid, robot.current_node, idle_wait)
            return {
                "type": "cmd_ack_response",
                "success": True,
                "action": "waiting_shelf_available",
                "shelf_id": next_st.shelf_id,
            }
        # else "go": IN_PLACE + 예약 없음 → 그대로 진행
        return None

    def _get_shelf_availability(self, shelf_id: int, exclude_rid: int) -> str:
        """F 노드: 선반 배정 가능 상태 반환

        Returns:
            'direct' : AT_WORKSTATION + carrier NOT WAITING_FOR_PICK → WS 직행 가능
            'pending': 사용 불가 (CARRIED / AT_WS+WAITING_FOR_PICK / IN_PLACE+reserved)
            'go'     : IN_PLACE + 예약 없음 → 바로 이동 가능
        """
        shelf_obj = self.shelf_manager.get_shelf(shelf_id)
        if not shelf_obj:
            return "go"

        if shelf_obj.status == ShelfStatus.AT_WORKSTATION:
            carrier = self.robot_manager.get_robot(shelf_obj.carried_by) if shelf_obj.carried_by else None
            if carrier and carrier.status == RobotStatus.WAITING_FOR_PICK:
                return "pending"  # 다른 로봇이 이 선반으로 픽업 대기 중
            # Bug B fix: WS 회랑이 다른 로봇에 의해 점유 중이면 진입 불가 → PENDING
            ws_node = shelf_obj.current_node
            corridor = self.staging_manager.corridors.get(ws_node)
            if (corridor and corridor.state == CorridorState.OCCUPIED
                    and corridor.occupying_rid != exclude_rid):
                return "pending"
            return "direct"

        elif shelf_obj.status == ShelfStatus.CARRIED:
            return "pending"

        else:  # IN_PLACE
            if self._is_shelf_targeted_by_moving_robot(shelf_id, exclude_rid):
                return "pending"  # 다른 AGV가 이미 이 선반으로 이동 중
            return "go"

    def _reroute_robot_after_skip(self, task_id: str, new_current_st) -> None:
        """포워딩 스킵 후 T2의 로봇을 새 서브태스크 목적지로 재라우팅

        T2의 선반 서브태스크가 스킵된 후 T2의 로봇이 이미 이동 중이면
        새 목적지로 경로를 재계획한다.
        """
        task = self.task_manager.get_task(task_id)
        if not task or not task.assigned_robot:
            return

        robot = self.robot_manager.get_robot(task.assigned_robot)
        if not robot:
            return

        if new_current_st is None:
            # 작업 완료: 로봇 → IDLE → 홈 staging 노드 대기
            self._clear_robot_reservation(robot.rid)
            self.robot_manager.complete_task(robot.rid)
            idle_wait = self._get_idle_wait_node(robot.rid)
            if robot.current_node != idle_wait:
                self._plan_and_publish_move(robot.rid, robot.current_node, idle_wait)
            print(f"[RequestHandler] T2(robot {robot.rid}): task complete after skip, → staging")
            return

        if new_current_st.subtask_type == SubTaskType.GO_TO_SHELF:
            # 다음 선반으로 재라우팅
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
            self._plan_and_publish_move(robot.rid, robot.current_node, new_current_st.target_node)
            print(f"[RequestHandler] T2(robot {robot.rid}): rerouted to shelf "
                  f"{new_current_st.shelf_id} node {new_current_st.target_node}")
