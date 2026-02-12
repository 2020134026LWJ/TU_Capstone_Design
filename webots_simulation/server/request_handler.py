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
from .shelf_manager import ShelfManager
from .staging_manager import StagingManager
from .task_manager import TaskManager, SubTaskType, TaskStatus


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

        # 로봇별 계획된 경로 (충돌 회피용)
        # rid -> timed_path [(node, time), ...]
        self._robot_planned_paths: Dict[int, List[Tuple[int, int]]] = {}

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
            "task_request": self._handle_task_request,
            "batch_task_request": self._handle_batch_task,
            "pick_complete": self._handle_pick_complete,
            "robot_arrived": self._handle_robot_arrived,
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
                break

            # 작업 시작
            first_st = self.task_manager.start_task(task.task_id, robot.rid)
            if not first_st:
                break

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

    # ─── 로봇 도착 처리 ───

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

        return self._process_arrival(robot, task, current_st)

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
            # 포워딩 완료 → 선반 내려놓기
            self.shelf_manager.mark_shelf_at_workstation(shelf_id, current_st.target_node)
            self.robot_manager.set_carrying_shelf(robot.rid, None)

            # 다른 작업에 선반 포워딩 알림 → 해당 작업의 GO_TO_SHELF 목적지 수정
            forwarded_task_id = self.task_manager.handle_shelf_forwarded(
                shelf_id, current_st.target_node
            )
            if forwarded_task_id:
                print(f"[RequestHandler] Shelf {shelf_id} forwarded, "
                      f"updated task {forwarded_task_id}")

            # 현재 서브태스크 완료 → 다음 진행
            result = self.task_manager.handle_subtask_complete(task.task_id)

            if result.get("action") == "task_complete":
                # 모든 서브태스크 완료
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
                if next_st and next_st.subtask_type == SubTaskType.GO_TO_SHELF:
                    # 다음 선반으로 이동
                    self.robot_manager.set_robot_status(
                        robot.rid, RobotStatus.MOVING_TO_SHELF
                    )
                    self._plan_and_publish_move(
                        robot.rid, robot.current_node, next_st.target_node
                    )
                    return {
                        "type": "shelf_ack_response",
                        "success": True,
                        "action": "moving_to_next_shelf",
                        "target_node": next_st.target_node,
                    }
                elif next_st and next_st.subtask_type == SubTaskType.WAIT_PICKING:
                    self.robot_manager.set_robot_status(
                        robot.rid, RobotStatus.WAITING_FOR_PICK
                    )
                    return {
                        "type": "shelf_ack_response",
                        "success": True,
                        "action": "wait_picking_at_forward_ws",
                        "shelf_id": shelf_id,
                        "items_to_pick": next_st.items_to_pick,
                    }
        else:
            print(f"[RequestHandler] Warning: putdown ack but current subtask is {st_type}")
            return {"type": "shelf_ack_response", "success": False, "error": "not_returning"}

        return {"type": "shelf_ack_response", "success": True, "action": "putdown_done"}

    # ─── 물품 픽업 완료 ───

    def _handle_pick_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        작업자가 물품 픽업 완료 신호

        요청:
        {"type": "pick_complete", "task_id": "T1", "item": "A", "workstation_id": 33}
        """
        task_id = data.get("task_id")
        item = data.get("item")

        if not task_id or not item:
            return self._error_response("Missing 'task_id' or 'item'")

        result = self.task_manager.handle_item_picked(task_id, item)

        if result.get("action") == "continue_picking":
            return {
                "type": "pick_complete_response",
                "success": True,
                "task_id": task_id,
                "item": item,
                "action": "continue_picking",
                "remaining_items_on_shelf": result.get("remaining_items_on_shelf", []),
                "total_remaining": result.get("total_remaining", []),
            }

        elif result.get("action") == "shelf_done":
            # 선반의 모든 필요 물품 픽업 완료 → 로봇 이동
            shelf_id = result.get("shelf_id")
            next_action = result.get("next_action")

            task = self.task_manager.get_task(task_id)
            if not task:
                return self._error_response("Task not found after shelf_done")

            robot = self.robot_manager.get_robot_carrying_shelf(shelf_id) if shelf_id else None

            if robot and next_action == "return":
                return_to = result.get("return_to", shelf_id)
                # Point C: AGV 작업대 퇴출 → 회랑 exiting 표시
                self.staging_manager.mark_exiting(robot.current_node, robot.rid)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.RETURNING_SHELF)
                self._plan_and_publish_move(robot.rid, robot.current_node, return_to)

                return {
                    "type": "pick_complete_response",
                    "success": True,
                    "task_id": task_id,
                    "item": item,
                    "action": "shelf_done",
                    "next_action": "return_shelf",
                    "return_to": return_to,
                    "robot_id": robot.rid,
                }

            elif robot and next_action == "forward":
                forward_ws = result.get("forward_to_ws")
                # Point C: AGV 작업대 퇴출 → 회랑 exiting 표시
                self.staging_manager.mark_exiting(robot.current_node, robot.rid)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.DELIVERING_TO_WS)
                self._plan_and_publish_move(robot.rid, robot.current_node, forward_ws)

                return {
                    "type": "pick_complete_response",
                    "success": True,
                    "task_id": task_id,
                    "item": item,
                    "action": "shelf_done",
                    "next_action": "forward_shelf",
                    "forward_to_ws": forward_ws,
                    "robot_id": robot.rid,
                }

        return {
            "type": "pick_complete_response",
            "success": False,
            "error": result.get("message", "Unknown error"),
        }

    # ─── 기존 호환 요청 ───

    def _handle_task_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        단건 작업 요청 (기존 호환)

        요청:
        {
            "type": "task_request",
            "worker_id": 1,
            "worker_marker": 33,
            "shelf_marker": 15
        }
        """
        worker_id = data.get("worker_id")
        worker_marker = data.get("worker_marker")
        shelf_marker = data.get("shelf_marker")

        if worker_id is None:
            return self._error_response("Missing 'worker_id'")
        if worker_marker is None:
            return self._error_response("Missing 'worker_marker'")
        if shelf_marker is None:
            return self._error_response("Missing 'shelf_marker'")

        start_node = int(worker_marker)
        goal_node = int(shelf_marker)

        if not self.path_planner.is_valid_node(start_node):
            return self._error_response(f"Invalid worker_marker: {worker_marker}")
        if not self.path_planner.is_valid_node(goal_node):
            return self._error_response(f"Invalid shelf_marker: {shelf_marker}")

        robot = self.robot_manager.get_robot_by_worker(worker_id)
        if not robot:
            return self._error_response(f"Robot not found for worker_id: {worker_id}")

        self.robot_manager.update_robot_position(robot.rid, start_node)

        timed_path = self.path_planner.plan_single_robot(
            start=start_node,
            goal=goal_node,
            max_time=self.config.max_time,
        )

        if timed_path is None:
            return self._error_response(f"No path found: {start_node} -> {goal_node}")

        node_path = PathPlanner.compress_to_node_path(timed_path)

        task = {
            "task_id": f"legacy_{int(time.time())}",
            "worker_id": worker_id,
            "start_node": start_node,
            "goal_node": goal_node,
            "node_path": node_path,
            "timestamp": time.time(),
        }

        self.robot_manager.assign_task(robot.rid, task)

        mqtt_success = self.mqtt_publisher.publish_single_robot_plan(
            rid=robot.rid,
            start=start_node,
            goal=goal_node,
            timed_path=timed_path,
            speed=self.config.default_speed,
        )

        return {
            "type": "task_response",
            "success": True,
            "worker_id": worker_id,
            "robot_id": robot.rid,
            "start_node": start_node,
            "goal_node": goal_node,
            "path": node_path,
            "path_length": len(node_path),
            "mqtt_published": mqtt_success,
        }

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

            # 예약 등록
            for i in range(len(remaining)):
                node_i, _ = remaining[i]
                reserved_nodes.add((node_i, i))

                if i + 1 < len(remaining):
                    node_j, _ = remaining[i + 1]
                    if node_j != node_i:
                        reserved_edges.add((node_i, node_j, i))

            # 목표 도착 후 대기 예약
            if remaining:
                goal_node, _ = remaining[-1]
                goal_t = len(remaining) - 1
                for dt in range(1, 4):
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

    def _get_occupied_shelf_nodes(self) -> Set[int]:
        """현재 선반이 놓여있는 노드 집합 (IN_PLACE 상태인 선반)"""
        from .shelf_manager import ShelfStatus
        occupied = set()
        for shelf in self.shelf_manager.shelves.values():
            if shelf.status == ShelfStatus.IN_PLACE:
                occupied.add(shelf.current_node)
        return occupied

    def _plan_and_publish_move(
        self, rid: int, start: int, goal: int
    ) -> Optional[Dict]:
        """로봇 이동 경로 계획 및 MQTT 발행 (다른 로봇 경로 회피)"""
        # start==goal: 이미 목적지에 있으면 즉시 도착 처리
        if start == goal:
            print(f"[RequestHandler] Robot {rid}: already at goal {goal}, immediate arrival")
            self._robot_planned_paths.pop(rid, None)
            self._handle_robot_arrived({
                "type": "robot_arrived", "rid": rid, "node": goal,
            })
            return {
                "rid": rid, "start": start, "goal": goal,
                "path_length": 0, "mqtt_published": False,
            }

        # Point A: 작업대 스테이징 체크
        actual_goal = goal
        if goal in self.staging_manager.corridors:
            staging_node = self.staging_manager.should_stage(goal, rid)
            if staging_node is not None:
                # 회랑 점유 중 → 스테이징 노드로 우회
                actual_goal = staging_node
                self.staging_manager.add_staged_agv(goal, rid, staging_node)
                print(f"[RequestHandler] Robot {rid}: redirected to staging node {staging_node} "
                      f"(target WS {goal})")

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
        return released

    def _error_response(self, message: str) -> Dict[str, Any]:
        """에러 응답"""
        print(f"[RequestHandler] Error: {message}")
        return {"type": "error", "success": False, "error": message}
