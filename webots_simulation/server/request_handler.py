"""
요청 처리 모듈
- JSON 파싱 및 검증
- 요청 타입별 라우팅
- 배치 작업 등록, 물품 픽업 완료, 로봇 도착 처리
"""

import json
import time
from typing import Any, Dict, Optional, List, Set, Tuple

from .config import Config
from .path_planner import PathPlanner
from .mqtt_publisher import MQTTPublisher
from .robot_manager import RobotManager, RobotStatus
from .shelf_manager import ShelfManager, ShelfStatus
from .staging_manager import StagingManager, CorridorState
from .task_manager import TaskManager, SubTaskType, TaskStatus
from .db_loader import DBLoader
from .task_scheduler import TaskScheduler


class RequestHandler:
    """요청 처리기"""

    def __init__(
        self,
        config: Config,
        path_planner: PathPlanner,
        mqtt_publisher: MQTTPublisher,
        robot_manager: RobotManager,
        shelf_manager: ShelfManager,
        staging_manager: StagingManager,
        task_manager: TaskManager,
    ):
        self.config = config
        self.path_planner = path_planner
        self.mqtt_publisher = mqtt_publisher
        self.robot_manager = robot_manager
        self.shelf_manager = shelf_manager
        self.staging_manager = staging_manager
        self.task_manager = task_manager

        # DB 로더 + 작업 스케줄러
        import os
        db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Database")
        self.db_loader = DBLoader(db_dir)
        self.task_scheduler = TaskScheduler(self.db_loader)

        # 로봇별 계획된 경로 [rid → timed_path [(node, time), ...]]
        self._robot_planned_paths: Dict[int, List[Tuple[int, int]]] = {}

        # 포워딩으로 소스 회랑이 미리 해제됐으나 아직 스테이징 노드에 미도착한 로봇
        # rid → target_ws (스테이징 노드 도착 시 이 WS로 이동 명령 발행)
        self._staged_to_ws: Dict[int, int] = {}

        # 서버 기반 충돌 회피: NODE_WAIT 중인 로봇 + 다음 노드 점유 추적
        self._waiting_robots: Set[int] = set()   # resume 대기 중인 로봇
        self._claimed_nodes: Dict[int, int] = {} # node → rid (이동 예약된 노드)

        # 포워딩된 선반의 재픽업+반납을 담당하는 로봇
        # shelf_id → rid (pick_complete 후 re-pickup 시 사용)
        self._forwarded_shelf_handlers: Dict[int, int] = {}

        # 브로드캐스트 콜백 (WebSocketHandler에서 설정)
        self._broadcast_callback = None

    def set_broadcast_callback(self, callback):
        """WebSocket 브로드캐스트 콜백 설정"""
        self._broadcast_callback = callback

    async def _broadcast(self, message: dict):
        """브로드캐스트 헬퍼"""
        if self._broadcast_callback:
            await self._broadcast_callback(message)

    def handle_message(self, message: str) -> Dict[str, Any]:
        """메시지 처리 (동기)"""
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            return self._error_response(f"Invalid JSON: {e}")

        msg_type = data.get("type")
        if not msg_type:
            return self._error_response("Missing 'type' field")

        handlers = {
            "start_order": self._handle_start_order,
            "batch_task_request": self._handle_batch_task,
            "shelf_complete": self._handle_shelf_complete,
            "order_complete": self._handle_order_complete,
            "robot_arrived": self._handle_robot_arrived,
            "robot_position": self._handle_robot_position,
            "shelf_ack": self._handle_shelf_ack,
            "status_request": self._handle_status_request,
            "task_status_request": self._handle_task_status,
            "shelf_status_request": self._handle_shelf_status,
            "robot_status": self._handle_robot_status,
        }

        handler = handlers.get(msg_type)
        if not handler:
            return self._error_response(f"Unknown request type: {msg_type}")

        return handler(data)

    # ─── 주문 시작 (엑셀 DB 기반) ───

    def _handle_start_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        주문 시작 (엑셀 DB에서 로드 → TaskScheduler 최적화 → 작업 등록)

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

        schedule = self.task_scheduler.schedule_order(user_id=user_id, order_id=order_id)
        if not schedule:
            return self._error_response(f"Order not found: user={user_id}, order={order_id}")

        workstation_id = schedule["workstation"]
        shelf_sequence = schedule["shelf_sequence"]
        item_names = []
        for t in schedule["tasks"]:
            item_names.extend(t.items)

        task_id = f"T{user_id}_{order_id}"
        task = self.task_manager.create_task(
            task_id=task_id,
            workstation_id=workstation_id,
            items=item_names,
            optimized_shelf_sequence=shelf_sequence,
        )

        if not task:
            return self._error_response("Failed to create task")

        msgs = self._try_assign_pending_tasks()

        # _try_assign_pending_tasks가 shelf 순서를 로테이션했을 수 있으므로
        # 실제 task.shelf_sequence 기준으로 응답 구성 (CLI-서버 순서 동기화)
        task_obj = self.task_manager.get_task(task_id)
        if task_obj:
            schedule_map = {t.shelf_node: t for t in schedule["tasks"]}
            wait_picking_items: Dict[int, List] = {}
            for st in task_obj.subtasks:
                if (st.subtask_type == SubTaskType.WAIT_PICKING
                        and st.shelf_id not in wait_picking_items):
                    wait_picking_items[st.shelf_id] = st.items_to_pick
            shelves = [
                {
                    "order": idx + 1,
                    "shelf_label": schedule_map[sid].shelf_label if sid in schedule_map else str(sid),
                    "shelf_node": sid,
                    "items": wait_picking_items.get(sid, []),
                }
                for idx, sid in enumerate(task_obj.shelf_sequence)
            ]
        else:
            shelves = [
                {
                    "order": t.order,
                    "shelf_label": t.shelf_label,
                    "shelf_node": t.shelf_node,
                    "items": t.items,
                }
                for t in schedule["tasks"]
            ]

        return {
            "type": "start_order_response",
            "success": True,
            "task_id": task_id,
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

    def _try_assign_pending_tasks(self) -> List[Dict]:
        """대기 중인 작업에 유휴 로봇 할당 시도"""
        assignments = []
        rotation_counts: Dict[str, int] = {}  # Bug A: 선반 순서 변경 시도 횟수

        while True:
            task = self.task_manager.get_next_pending_task()
            if not task:
                break

            # 첫 번째 선반의 위치를 기준으로 가장 가까운 로봇 배정
            first_shelf = task.shelf_sequence[0] if task.shelf_sequence else None
            robot = self.robot_manager.get_available_robot(
                target_node=first_shelf,
                path_planner=self.path_planner,
            )
            if not robot:
                if self._try_intercept_returning_shelf(task):
                    continue  # 인터셉트 성공 → 다음 대기 작업 처리 시도
                break         # 인터셉트도 불가 → 종료

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
                    # Bug A: 다음 선반이 있으면 순서 변경 후 재시도
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
                    # 모든 선반 차단 또는 단일 선반 → PENDING
                    rotation_counts.pop(task.task_id, None)
                    task.status = TaskStatus.PENDING
                    task.assigned_robot = None
                    first_st.status = TaskStatus.PENDING
                    print(f"[RequestHandler] F-node: shelf {actual_first_shelf} unavailable "
                          f"(status={shelf_obj.status.value}), task {task.task_id} → PENDING")
                    break
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
        """
        first_shelf = task.shelf_sequence[0] if task.shelf_sequence else None
        if first_shelf is None:
            return False

        carrying_robot = self.robot_manager.get_robot_carrying_shelf(first_shelf)
        if not carrying_robot:
            return False

        if carrying_robot.status != RobotStatus.RETURNING_SHELF:
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

        # 피드백 1: 로봇이 이전 WS 퇴출 중이었다면 해당 회랑을 해제
        # (인터셉트로 트리거 노드를 통과하지 못하므로 직접 해제)
        for ws_node, corridor in self.staging_manager.corridors.items():
            if (corridor.occupying_rid == carrying_robot.rid and
                    corridor.state == CorridorState.OCCUPIED):
                if corridor.queue:
                    released = corridor.queue.popleft()
                    corridor.occupying_rid = released.rid
                    corridor.occupied_at = time.time()
                    released_robot = self.robot_manager.get_robot(released.rid)
                    if released_robot:
                        self._plan_and_publish_move(
                            released_robot.rid, released.staging_node, ws_node
                        )
                    print(f"[RequestHandler] W{ws_node}: staged AGV-{released.rid} released "
                          f"(AGV-{carrying_robot.rid} intercepted)")
                else:
                    corridor.state = CorridorState.FREE
                    corridor.occupying_rid = None
                    print(f"[RequestHandler] W{ws_node}: corridor freed "
                          f"(AGV-{carrying_robot.rid} intercepted and redirected)")
                break

        # 피드백 3: MQTT 재발행으로 복귀 중인 로봇에게 새 경로 전달
        # (로봇은 새 plan 메시지를 수신하면 현재 이동을 중단하고 새 목적지로 향함)
        self._plan_and_publish_move(carrying_robot.rid, carrying_robot.current_node, target_ws)

        print(f"[RequestHandler] Node U: Robot {carrying_robot.rid} intercepted while returning "
              f"shelf {first_shelf} → redirecting to WS {target_ws} for task {task.task_id}")
        return True

    # ─── 로봇 도착 처리 ───

    def _handle_robot_position(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """중간 노드 위치 업데이트 → current_node 갱신 + 충돌 감지 후 resume 발행"""
        rid = data.get("rid")
        node = data.get("node")
        if rid is None or node is None:
            return {}

        self.robot_manager.update_robot_position(rid, node)

        # 이 로봇이 이전에 예약했던 노드 claim 해제
        self._claimed_nodes = {k: v for k, v in self._claimed_nodes.items() if v != rid}

        # 위치 기반 회랑 자동 해제:
        # 퇴출 중(is_exiting=True) 로봇이 회랑 구역(WS+게이트웨이) 밖으로 나가면 즉시 해제.
        # ArUco 트리거 경로에 없는 경우에도 동작 (handle_marker_trigger의 보완).
        released = self.staging_manager.check_position_release(rid, node)
        if released is not None:
            self._plan_and_publish_move(released.rid, released.staging_node, released.target_ws)
        else:
            self._try_assign_pending_tasks()

        # NODE_WAIT 등록 후 전체 대기 로봇 resume 시도
        self._waiting_robots.add(rid)
        self._try_resume_waiting_robots()
        return {}

    def _get_next_planned_node(self, rid: int) -> Optional[int]:
        """planned_path에서 current_node 이후 다음 노드 반환"""
        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return None
        planned_path = self._robot_planned_paths.get(rid)
        if not planned_path:
            return None
        cur = robot.current_node
        for i, (node, _) in enumerate(planned_path):
            if node == cur and i + 1 < len(planned_path):
                return planned_path[i + 1][0]
        return None

    def _is_safe_to_resume(self, rid: int) -> bool:
        """rid 로봇이 다음 노드로 이동해도 충돌이 없는지 확인"""
        next_node = self._get_next_planned_node(rid)
        if next_node is None:
            return False  # 다음 노드 없음 (goal이면 arrived로 처리됨)

        # 다른 로봇이 next_node를 예약(claim)했는지
        claimer = self._claimed_nodes.get(next_node)
        if claimer is not None and claimer != rid:
            return False

        for other_rid, other_robot in self.robot_manager.robots.items():
            if other_rid == rid:
                continue
            if other_robot.status == RobotStatus.IDLE:
                continue

            other_current = other_robot.current_node

            # 다른 로봇이 next_node에 있는 경우
            if other_current == next_node:
                # 그 로봇이 이미 다른 노드로 이동 예약(claim)됐으면 → 떠나는 중 → 안전
                other_claimed = next(
                    (n for n, r in self._claimed_nodes.items() if r == other_rid), None
                )
                if other_claimed is None:
                    # 상대가 아직 next_node에 물리적으로 있음 → 우선순위 무관하게 대기
                    # (우선순위는 같은 빈 노드를 동시에 요청할 때만 적용)
                    return False

        return True

    def _try_resume_waiting_robots(self):
        """대기 중인 로봇들에게 안전한 경우 resume 발행 (rid 오름차순 = 높은 우선순위 먼저)"""
        for rid in sorted(self._waiting_robots):
            if self._is_safe_to_resume(rid):
                next_node = self._get_next_planned_node(rid)
                if next_node is not None:
                    self._claimed_nodes[next_node] = rid
                self._waiting_robots.discard(rid)
                self.mqtt_publisher.publish_resume(rid)
                print(f"[RequestHandler] Robot {rid}: resume → node {next_node}")

    def _handle_robot_arrived(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        로봇이 목적지에 도착했을 때 처리 (bridge에서 수신)

        요청:
        {"type": "robot_arrived", "rid": 1, "node": 9}
        """
        rid = data.get("rid")
        arrived_node = data.get("node")

        if rid is None or arrived_node is None:
            return self._error_response("Missing 'rid' or 'node'")

        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return self._error_response(f"Robot {rid} not found")

        self.robot_manager.update_robot_position(rid, arrived_node)

        # Point B-1: 포워딩으로 소스 회랑이 미리 해제된 로봇
        # 스테이징 노드에 도착하면 즉시 목표 WS로 이동 명령 발행 (deferred release)
        if rid in self._staged_to_ws:
            target_ws = self._staged_to_ws[rid]
            staging_ws = self.staging_manager.get_ws_for_staging_node(arrived_node)
            if staging_ws == target_ws:
                del self._staged_to_ws[rid]
                print(f"[RequestHandler] Robot {rid}: pre-released, arrived at staging "
                      f"{arrived_node} → proceeding to W{target_ws}")
                self._plan_and_publish_move(rid, arrived_node, target_ws)
                return {"type": "robot_arrived_ack", "success": True, "action": "staging_released_proceed"}

        # Point B: 스테이징 대기 확인 (staging 노드 도착 시 작업 처리 없이 대기)
        if self.staging_manager.is_staged_agv(arrived_node, rid):
            print(f"[RequestHandler] Robot {rid}: staging at node {arrived_node}, waiting for corridor")
            return {"type": "robot_arrived_ack", "success": True, "action": "staging_wait"}

        task_id = robot.current_task_id
        if not task_id:
            return {"type": "robot_arrived_ack", "success": True, "action": "no_task"}

        task = self.task_manager.get_task(task_id)
        if not task:
            return {"type": "robot_arrived_ack", "success": True, "action": "task_not_found"}

        current_st = task.get_current_subtask()
        if not current_st:
            return {"type": "robot_arrived_ack", "success": True, "action": "no_subtask"}

        result = self._process_arrival(robot, task, current_st)
        # 로봇이 goal에 도착해 노드를 비웠으므로 대기 로봇 resume 재시도
        self._claimed_nodes = {k: v for k, v in self._claimed_nodes.items() if v != rid}
        self._try_resume_waiting_robots()
        return result

    def _process_arrival(self, robot, task, current_st) -> Dict[str, Any]:
        """로봇 도착 후 서브태스크 유형에 따라 처리"""

        st_type = current_st.subtask_type

        if st_type == SubTaskType.GO_TO_SHELF:
            # 선반에 도착 → shelf_cmd "pickup" 발행 (shelf_ack 대기)
            result = self.task_manager.handle_subtask_complete(task.task_id)
            next_st = task.get_current_subtask()

            if next_st and next_st.subtask_type == SubTaskType.PICKUP_SHELF:
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.PICKING_UP_SHELF)
                self.shelf_manager.mark_shelf_picked_up(next_st.shelf_id, robot.rid)
                self.robot_manager.set_carrying_shelf(robot.rid, next_st.shelf_id)

                # shelf_cmd "pickup" 발행 → AGV 리프트 올림 → shelf_ack 대기
                self.mqtt_publisher.publish_shelf_command(
                    rid=robot.rid, command="pickup", shelf_id=next_st.shelf_id
                )
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
                return {
                    "type": "robot_arrived_ack",
                    "success": True,
                    "action": "wait_picking",
                    "shelf_id": next_st.shelf_id,
                    "items_to_pick": next_st.items_to_pick,
                }

        elif st_type in (SubTaskType.RETURN_SHELF, SubTaskType.FORWARD_SHELF):
            # 선반 복귀/포워딩 목적지 도착 → shelf_cmd "putdown" 발행 (shelf_ack 대기)
            shelf_id = current_st.shelf_id
            self.mqtt_publisher.publish_shelf_command(
                rid=robot.rid, command="putdown", shelf_id=shelf_id
            )
            action = "waiting_shelf_putdown" if st_type == SubTaskType.RETURN_SHELF else "waiting_shelf_putdown_forward"
            return {
                "type": "robot_arrived_ack",
                "success": True,
                "action": action,
                "shelf_id": shelf_id,
            }

        return {"type": "robot_arrived_ack", "success": True, "action": "unknown_state"}

    # ─── 선반 리프트 완료 (shelf_ack) ───

    def _handle_shelf_ack(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        AGV 선반 리프트 동작 완료 (pickup/putdown)

        요청:
        {"type": "shelf_ack", "rid": 1, "command": "pickup", "shelf_id": 11, "status": "done"}
        """
        rid = data.get("rid")
        command = data.get("command")
        shelf_id = data.get("shelf_id")

        if rid is None or command is None:
            return self._error_response("Missing 'rid' or 'command' in shelf_ack")

        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return self._error_response(f"Robot {rid} not found")

        task_id = robot.current_task_id
        task = self.task_manager.get_task(task_id) if task_id else None
        if not task:
            return {"type": "shelf_ack_response", "success": True, "action": "no_task"}

        current_st = task.get_current_subtask()
        if not current_st:
            return {"type": "shelf_ack_response", "success": True, "action": "no_subtask"}

        if command == "pickup":
            return self._handle_pickup_ack(robot, task, current_st)
        elif command == "putdown":
            return self._handle_putdown_ack(robot, task, current_st)

        return {"type": "shelf_ack_response", "success": True, "action": "unknown_command"}

    def _handle_pickup_ack(self, robot, task, current_st) -> Dict[str, Any]:
        """pickup ack → PICKUP_SHELF 완료 → DELIVER_TO_WS 시작"""
        if current_st.subtask_type != SubTaskType.PICKUP_SHELF:
            print(f"[RequestHandler] Warning: pickup ack but current subtask is {current_st.subtask_type}")
            return {"type": "shelf_ack_response", "success": False, "error": "not_picking_up"}

        # PICKUP_SHELF 완료 → DELIVER_TO_WS
        result = self.task_manager.handle_subtask_complete(task.task_id)
        next_st = task.get_current_subtask()

        if next_st and next_st.subtask_type == SubTaskType.DELIVER_TO_WS:
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.DELIVERING_TO_WS)
            self._plan_and_publish_move(
                robot.rid, robot.current_node, next_st.target_node
            )
            return {
                "type": "shelf_ack_response",
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
                "type": "shelf_ack_response",
                "success": True,
                "action": "returning_forwarded_shelf",
                "shelf_id": shelf_id,
                "return_to": return_node,
            }

        return {"type": "shelf_ack_response", "success": True, "action": "pickup_done"}

    def _handle_putdown_ack(self, robot, task, current_st) -> Dict[str, Any]:
        """putdown ack → RETURN_SHELF 또는 FORWARD_SHELF 완료 처리"""
        st_type = current_st.subtask_type
        shelf_id = current_st.shelf_id

        if st_type == SubTaskType.RETURN_SHELF:
            # 선반 반납 완료
            self.shelf_manager.mark_shelf_returned(shelf_id, current_st.target_node)
            self.robot_manager.set_carrying_shelf(robot.rid, None)

            result = self.task_manager.handle_subtask_complete(task.task_id)

            if result.get("action") == "task_complete":
                self.robot_manager.complete_task(robot.rid)
                self._robot_planned_paths.pop(robot.rid, None)
                self._try_assign_pending_tasks()
                # 새 작업이 없으면 홈 작업대로 복귀
                if robot.status == RobotStatus.IDLE and robot.current_node != robot.home_node:
                    self._plan_and_publish_move(robot.rid, robot.current_node, robot.home_node)
                return {
                    "type": "shelf_ack_response",
                    "success": True,
                    "action": "task_complete",
                    "task_id": task.task_id,
                }
            elif result.get("action") == "next_subtask":
                next_st = task.get_current_subtask()
                if next_st:
                    # F 노드: 다음 선반 현재 위치 체크 (5분기)
                    next_shelf_obj = self.shelf_manager.get_shelf(next_st.shelf_id) if next_st.shelf_id else None
                    if next_shelf_obj:
                        avail = self._get_shelf_availability(next_st.shelf_id, robot.rid)
                        if avail == "direct":
                            next_st.target_node = next_shelf_obj.current_node
                            print(f"[RequestHandler] F-node(return): shelf {next_st.shelf_id} at WS "
                                  f"{next_shelf_obj.current_node}, task {task.task_id} → direct to WS")
                        elif avail == "pending":
                            task.status = TaskStatus.PENDING
                            task.assigned_robot = None
                            next_st.status = TaskStatus.PENDING
                            self.robot_manager.set_robot_status(robot.rid, RobotStatus.IDLE)
                            robot.current_task_id = None
                            self._robot_planned_paths.pop(robot.rid, None)
                            print(f"[RequestHandler] F-node(return): shelf {next_st.shelf_id} unavailable "
                                  f"(status={next_shelf_obj.status.value}), "
                                  f"task {task.task_id} → PENDING, robot {robot.rid} → IDLE")
                            self._try_assign_pending_tasks()
                            if robot.status == RobotStatus.IDLE and robot.current_node != robot.home_node:
                                self._plan_and_publish_move(robot.rid, robot.current_node, robot.home_node)
                            return {
                                "type": "shelf_ack_response",
                                "success": True,
                                "action": "waiting_shelf_available",
                                "shelf_id": next_st.shelf_id,
                            }
                        # else "go": IN_PLACE + 예약 없음 → 그대로 진행
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
                    self._plan_and_publish_move(
                        robot.rid, robot.current_node, next_st.target_node
                    )
                    return {
                        "type": "shelf_ack_response",
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
                self.robot_manager.complete_task(robot.rid)
                self._robot_planned_paths.pop(robot.rid, None)
                self._try_assign_pending_tasks()
                if robot.status == RobotStatus.IDLE and robot.current_node != robot.home_node:
                    self._plan_and_publish_move(robot.rid, robot.current_node, robot.home_node)
                return {
                    "type": "shelf_ack_response",
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
                        "type": "shelf_ack_response",
                        "success": True,
                        "action": "wait_picking_at_forward_ws",
                        "shelf_id": shelf_id,
                        "items_to_pick": next_st.items_to_pick,
                    }
                elif next_st and next_st.subtask_type == SubTaskType.GO_TO_SHELF:
                    # F 노드: 다음 선반 현재 위치 체크
                    next_shelf_obj = self.shelf_manager.get_shelf(next_st.shelf_id) if next_st.shelf_id else None
                    if next_shelf_obj:
                        avail = self._get_shelf_availability(next_st.shelf_id, robot.rid)
                        if avail == "direct":
                            next_st.target_node = next_shelf_obj.current_node
                            print(f"[RequestHandler] F-node(forward): shelf {next_st.shelf_id} at WS "
                                  f"{next_shelf_obj.current_node}, task {task.task_id} → direct to WS")
                        elif avail == "pending":
                            task.status = TaskStatus.PENDING
                            task.assigned_robot = None
                            next_st.status = TaskStatus.PENDING
                            self.robot_manager.set_robot_status(robot.rid, RobotStatus.IDLE)
                            robot.current_task_id = None
                            self._robot_planned_paths.pop(robot.rid, None)
                            print(f"[RequestHandler] F-node(forward): shelf {next_st.shelf_id} unavailable "
                                  f"(status={next_shelf_obj.status.value}), "
                                  f"task {task.task_id} → PENDING, robot {robot.rid} → IDLE")
                            self._try_assign_pending_tasks()
                            if robot.status == RobotStatus.IDLE and robot.current_node != robot.home_node:
                                self._plan_and_publish_move(robot.rid, robot.current_node, robot.home_node)
                            return {
                                "type": "shelf_ack_response",
                                "success": True,
                                "action": "waiting_shelf_available",
                                "shelf_id": next_st.shelf_id,
                            }
                        # else "go": 그대로 진행
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
                    self._plan_and_publish_move(robot.rid, robot.current_node, next_st.target_node)
                    self._try_assign_pending_tasks()
                    return {
                        "type": "shelf_ack_response",
                        "success": True,
                        "action": "moving_to_next_shelf",
                        "target_node": next_st.target_node,
                    }
        else:
            print(f"[RequestHandler] Warning: putdown ack but current subtask is {st_type}")
            return {"type": "shelf_ack_response", "success": False, "error": "not_returning"}

        return {"type": "shelf_ack_response", "success": True, "action": "putdown_done"}

    # ─── 물품 픽업 완료 ───

    def _handle_shelf_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        작업자가 WS 선반 픽업 완료 신호 → AGV 반납/포워딩 (GUI → 서버)

        요청: {"type": "shelf_complete", "사용자ID": 1}
        서버가 해당 사용자 WS의 AT_WORKSTATION 선반을 자동 탐색
        """
        user_id = data.get("사용자ID")
        if user_id is None:
            return self._error_response("Missing '사용자ID'")

        # user_id → 작업대 WS 노드 탐색 (IN_PROGRESS task 기준)
        user_task = next(
            (t for t in self.task_manager.tasks.values()
             if t.task_id.startswith(f"T{user_id}_") and
             t.status.value == "in_progress"),
            None
        )
        if not user_task:
            return self._error_response(f"No active task for user {user_id}")

        ws_node = user_task.workstation_id

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

                # Point C (포워딩): 경로 먼저 계획 후 소스 회랑 즉시 해제
                self._plan_and_publish_move(robot.rid, source_ws, forward_ws)

                released = self.staging_manager.release_corridor_without_trigger(
                    source_ws, robot.rid
                )
                if released:
                    released_robot = self.robot_manager.get_robot(released.rid)
                    if released_robot and released_robot.current_node == released.staging_node:
                        self._plan_and_publish_move(
                            released_robot.rid, released.staging_node, source_ws
                        )
                        print(f"[RequestHandler] W{source_ws}: AGV-{released.rid} already at "
                              f"staging {released.staging_node} → proceeding to WS immediately")
                    else:
                        self._staged_to_ws[released.rid] = source_ws
                        print(f"[RequestHandler] W{source_ws}: AGV-{released.rid} pre-released, "
                              f"will proceed when arriving at staging {released.staging_node}")
                return {"success": True, "action": "forwarding_shelf", "forward_to_ws": forward_ws}

        elif result.get("action") == "shelf_done_pickup_for_return":
            # 포워딩된 선반 픽업 완료 → 포워딩 로봇이 re-pickup 후 반납
            shelf_id_r = result.get("shelf_id")
            robot = self.robot_manager.get_robot(task.assigned_robot) if task else None

            if robot and shelf_id_r:
                self.shelf_manager.mark_shelf_picked_up(shelf_id_r, robot.rid)
                self.robot_manager.set_carrying_shelf(robot.rid, shelf_id_r)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.PICKING_UP_SHELF)
                self.mqtt_publisher.publish_shelf_command(
                    rid=robot.rid, command="pickup", shelf_id=shelf_id_r
                )
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

        task_id = f"T{user_id}_{order_id}"
        task = self.task_manager.get_task(task_id)
        if not task:
            return self._error_response(f"Task not found: {task_id}")

        print(f"[RequestHandler] order_complete: user={user_id}, task={task_id}, "
              f"status={task.status.value}")
        return {"success": True, "action": "order_complete", "task_id": task_id}

    # ─── 상태 조회 ───

    def _handle_status_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """시스템 상태 요청"""
        return {
            "type": "status_response",
            "success": True,
            "mqtt_connected": self.mqtt_publisher.is_connected(),
            "robots": self.robot_manager.get_status_summary(),
            "tasks": self.task_manager.get_status_summary(),
            "shelves": self.shelf_manager.get_status_summary(),
            "staging": self.staging_manager.get_status_summary(),
        }

    def _handle_task_status(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """작업 상태 조회"""
        task_id = data.get("task_id")
        if task_id:
            task = self.task_manager.get_task(task_id)
            if task:
                return {"type": "task_status_response", "success": True, "task": task.to_dict()}
            return self._error_response(f"Task {task_id} not found")

        return {
            "type": "task_status_response",
            "success": True,
            "tasks": self.task_manager.get_status_summary(),
        }

    def _handle_shelf_status(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """선반 상태 조회"""
        return {
            "type": "shelf_status_response",
            "success": True,
            **self.shelf_manager.get_status_summary(),
        }

    def _handle_robot_status(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """로봇 상태 업데이트 (bridge에서 수신)"""
        rid = data.get("rid")
        current_node = data.get("current_node")
        status = data.get("status")

        if rid is not None and current_node is not None:
            self.robot_manager.update_robot_position(rid, current_node)

        if rid is not None and status is not None:
            try:
                robot_status = RobotStatus(status)
                self.robot_manager.set_robot_status(rid, robot_status)
            except ValueError:
                pass

        return {"type": "robot_status_ack", "success": True}

    # ─── 유틸리티 ───

    def _get_other_robot_reservations(
        self, exclude_rid: int, goal_node: int = None
    ) -> Tuple[Set[Tuple[int, int]], Set[Tuple[int, int, int]]]:
        """다른 로봇의 남은 경로를 예약 노드/엣지로 변환

        이동 중인 로봇: planned_path 기반 예약
        정지 중인 로봇 (WAITING_FOR_PICK 등): 현재 위치를 제한된 시간 예약
        goal_node: 이 노드는 예약에서 제외 (도착 허용)
        """
        reserved_nodes: Set[Tuple[int, int]] = set()
        reserved_edges: Set[Tuple[int, int, int]] = set()
        max_time = self.config.max_time

        handled_rids = set()

        for other_rid, planned_path in self._robot_planned_paths.items():
            if other_rid == exclude_rid or not planned_path:
                continue

            robot = self.robot_manager.get_robot(other_rid)
            if not robot or robot.status == RobotStatus.IDLE:
                continue

            handled_rids.add(other_rid)

            # 다른 로봇의 현재 위치부터 남은 경로 추출
            remaining = self._estimate_remaining_timed_path(
                planned_path, robot.current_node
            )

            # 예약 등록 (+1 타임스텝 버퍼 — 회전 지연 등 실행 타이밍 오차 보정)
            for i in range(len(remaining)):
                node_i, _ = remaining[i]
                for dt in [0, 1]:
                    t_buf = i + dt
                    if t_buf < max_time:
                        reserved_nodes.add((node_i, t_buf))

                if i + 1 < len(remaining):
                    node_j, _ = remaining[i + 1]
                    if node_j != node_i:
                        reserved_edges.add((node_i, node_j, i))

            # 목표 도착 후 대기 예약
            if remaining:
                goal_node, _ = remaining[-1]
                goal_t = len(remaining) - 1
                for dt in range(1, 6):
                    reserved_nodes.add((goal_node, goal_t + dt))

        # 정지 중인 로봇 (planned path 없지만 점유 중)
        for other_rid, robot in self.robot_manager.robots.items():
            if other_rid == exclude_rid:
                continue
            if other_rid in handled_rids:
                continue
            if robot.status == RobotStatus.IDLE:
                continue
            # 목표 노드에 있는 로봇은 예약 스킵 (도착 허용)
            if goal_node is not None and robot.current_node == goal_node:
                continue
            # 정지 로봇의 현재 위치를 제한된 시간 예약 (과도한 차단 방지)
            reserve_window = min(12, max_time)
            for t in range(reserve_window):
                reserved_nodes.add((robot.current_node, t))

        return reserved_nodes, reserved_edges

    def _estimate_remaining_timed_path(
        self, timed_path: List[Tuple[int, int]], current_node: int
    ) -> List[Tuple[int, int]]:
        """로봇의 현재 위치부터 남은 경로를 t=0부터 재인덱싱"""
        # 현재 노드를 경로에서 찾기
        for i, (node, _t) in enumerate(timed_path):
            if node == current_node:
                remaining = timed_path[i:]
                # t=0부터 재인덱싱
                return [(n, j) for j, (n, _) in enumerate(remaining)]

        # 못 찾으면 전체 경로 반환 (로봇이 노드 사이에 있을 수 있음)
        return [(n, j) for j, (n, _) in enumerate(timed_path)]

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

    def _get_occupied_shelf_nodes(self) -> Set[int]:
        """현재 선반이 놓여있는 노드 집합 (IN_PLACE 상태인 선반)"""
        occupied = set()
        for shelf in self.shelf_manager.shelves.values():
            if shelf.status == ShelfStatus.IN_PLACE:
                occupied.add(shelf.current_node)
        return occupied

    def _plan_and_publish_move(
        self, rid: int, start: int, goal: int
    ) -> Optional[Dict]:
        """로봇 이동 경로 계획 및 MQTT 발행 (다른 로봇 경로 회피)"""
        # Point A: 작업대 스테이징 체크 (Bug B fix: start==goal보다 먼저 실행)
        # home=WS인 로봇이 같은 WS로 이동할 때 start==goal로 staging을 우회하는 버그 수정
        actual_goal = goal
        if goal in self.staging_manager.corridors:
            staging_node = self.staging_manager.should_stage(goal, rid)
            if staging_node is not None:
                # 회랑 점유 중 → 스테이징 노드로 우회
                actual_goal = staging_node
                self.staging_manager.add_staged_agv(goal, rid, staging_node)
                print(f"[RequestHandler] Robot {rid}: redirected to staging node {staging_node} "
                      f"(target WS {goal})")

        # start==actual_goal: 이미 목적지에 있으면 즉시 도착 처리
        if start == actual_goal:
            print(f"[RequestHandler] Robot {rid}: already at goal {actual_goal}, immediate arrival")
            self._robot_planned_paths.pop(rid, None)
            self._handle_robot_arrived({
                "type": "robot_arrived", "rid": rid, "node": actual_goal,
            })
            return {
                "rid": rid, "start": start, "goal": actual_goal,
                "path_length": 0, "mqtt_published": False,
            }

        # 타임아웃으로 해제된 대기 AGV 이동 명령 처리 (_check_timeout 에서 적재)
        if self.staging_manager.pending_timeout_releases:
            timeout_releases = list(self.staging_manager.pending_timeout_releases)
            self.staging_manager.pending_timeout_releases.clear()
            for t_released in timeout_releases:
                t_robot = self.robot_manager.get_robot(t_released.rid)
                if t_robot:
                    print(f"[RequestHandler] Timeout-release: sending AGV-{t_released.rid} "
                          f"from staging {t_released.staging_node} → WS {t_released.target_ws}")
                    self._plan_and_publish_move(
                        t_released.rid, t_released.staging_node, t_released.target_ws
                    )

        # 선반 운반 중이면 다른 선반이 놓인 노드 통과 불가
        robot = self.robot_manager.get_robot(rid)
        excluded_transit = None
        if robot and robot.carrying_shelf is not None:
            excluded_transit = self._get_occupied_shelf_nodes()

        # 다른 로봇의 경로 예약 수집 (actual_goal 노드는 예약 제외)
        reserved_nodes, reserved_edges = self._get_other_robot_reservations(rid, goal_node=actual_goal)

        timed_path = self.path_planner.astar_with_time(
            start=start,
            goal=actual_goal,
            reserved_nodes=reserved_nodes,
            reserved_edges=reserved_edges,
            max_time=self.config.max_time,
            excluded_transit=excluded_transit,
        )

        # 예약 회피 실패 시 최소 예약으로 재시도 (fallback)
        if timed_path is None:
            print(f"[RequestHandler] Robot {rid}: no path with reservations, trying with minimal")
            # 다른 로봇의 현재 위치만 t=0에서 회피 (즉시 충돌 방지)
            minimal_reserved = set()
            for other_rid, other_robot in self.robot_manager.robots.items():
                if other_rid == rid:
                    continue
                if other_robot.status != RobotStatus.IDLE and other_robot.current_node != actual_goal:
                    minimal_reserved.add((other_robot.current_node, 0))
            timed_path = self.path_planner.astar_with_time(
                start=start, goal=actual_goal,
                reserved_nodes=minimal_reserved, reserved_edges=set(),
                max_time=self.config.max_time,
                excluded_transit=excluded_transit,
            )

        if timed_path is None:
            print(f"[RequestHandler] No path found for Robot {rid}: {start} -> {actual_goal}")
            self._robot_planned_paths.pop(rid, None)
            return None

        # 계획된 경로 저장 (다음 로봇 계획 시 참조)
        self._robot_planned_paths[rid] = timed_path

        mqtt_success = self.mqtt_publisher.publish_single_robot_plan(
            rid=rid,
            start=start,
            goal=actual_goal,
            timed_path=timed_path,
            speed=self.config.default_speed,
        )

        node_path = PathPlanner.compress_to_node_path(timed_path)
        goal_desc = f"{actual_goal}" if actual_goal == goal else f"{actual_goal}(staging for {goal})"
        print(f"[RequestHandler] Robot {rid}: planned {start} -> {goal_desc}, "
              f"path={node_path}, MQTT={'ok' if mqtt_success else 'fail'}")

        return {
            "rid": rid,
            "start": start,
            "goal": actual_goal,
            "original_goal": goal,
            "path_length": len(timed_path),
            "mqtt_published": mqtt_success,
        }

    # ─── 스테이징 마커 트리거 ───

    def handle_marker_trigger(self, rid: int, marker_id: int):
        """Point D: 마커 인식 이벤트 → 스테이징 트리거 처리

        퇴출 AGV가 트리거 노드 마커를 인식하면 대기 AGV를 해제.
        """
        released = self.staging_manager.handle_marker_trigger(rid, marker_id)
        if released:
            # 대기 AGV를 작업대로 진입시킴
            self._plan_and_publish_move(
                released.rid, released.staging_node, released.target_ws
            )
        else:
            # Bug B fix: 대기 AGV 없이 회랑 FREE → PENDING 작업 재배정 시도
            # (AT_WORKSTATION + 회랑 점유로 PENDING됐던 작업이 이제 배정 가능)
            ws_node = self.staging_manager._trigger_to_ws.get(marker_id)
            if ws_node:
                corridor = self.staging_manager.corridors.get(ws_node)
                if corridor and corridor.state == CorridorState.FREE:
                    self._try_assign_pending_tasks()
        return released

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
            # 작업 완료: 로봇 → IDLE → 홈 복귀
            self.robot_manager.complete_task(robot.rid)
            self._robot_planned_paths.pop(robot.rid, None)
            if robot.current_node != robot.home_node:
                self._plan_and_publish_move(robot.rid, robot.current_node, robot.home_node)
            print(f"[RequestHandler] T2(robot {robot.rid}): task complete after skip, → home")
            return

        if new_current_st.subtask_type == SubTaskType.GO_TO_SHELF:
            # 다음 선반으로 재라우팅
            self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
            self._plan_and_publish_move(robot.rid, robot.current_node, new_current_st.target_node)
            print(f"[RequestHandler] T2(robot {robot.rid}): rerouted to shelf "
                  f"{new_current_st.shelf_id} node {new_current_st.target_node}")

    def _error_response(self, message: str) -> Dict[str, Any]:
        """에러 응답"""
        print(f"[RequestHandler] Error: {message}")
        return {"type": "error", "success": False, "error": message}
