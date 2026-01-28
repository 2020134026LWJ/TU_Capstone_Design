"""
요청 처리 모듈
- JSON 파싱 및 검증
- 요청 타입별 라우팅
- 배치 작업 등록, 물품 픽업 완료, 로봇 도착 처리
"""

import json
import time
from typing import Any, Dict, Optional, List

from .config import Config
from .path_planner import PathPlanner
from .mqtt_publisher import MQTTPublisher
from .robot_manager import RobotManager, RobotStatus
from .shelf_manager import ShelfManager
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
        task_manager: TaskManager,
    ):
        self.config = config
        self.path_planner = path_planner
        self.mqtt_publisher = mqtt_publisher
        self.robot_manager = robot_manager
        self.shelf_manager = shelf_manager
        self.task_manager = task_manager

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
                {"task_id": "T1", "workstation_id": 50, "items": ["A","B","Z","D"]},
                {"task_id": "T2", "workstation_id": 51, "items": ["C","X","U","I"]}
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
            # 선반에 도착 → 선반 들어올리기 단계로
            result = self.task_manager.handle_subtask_complete(task.task_id)
            next_st = task.get_current_subtask()

            if next_st and next_st.subtask_type == SubTaskType.PICKUP_SHELF:
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.PICKING_UP_SHELF)
                self.shelf_manager.mark_shelf_picked_up(next_st.shelf_id, robot.rid)
                self.robot_manager.set_carrying_shelf(robot.rid, next_st.shelf_id)

                # 픽업 완료 → 다음 단계 (배달)로 자동 진행
                result = self.task_manager.handle_subtask_complete(task.task_id)
                next_st = task.get_current_subtask()

                if next_st and next_st.subtask_type == SubTaskType.DELIVER_TO_WS:
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.DELIVERING_TO_WS)
                    self._plan_and_publish_move(
                        robot.rid, robot.current_node, next_st.target_node
                    )
                    return {
                        "type": "robot_arrived_ack",
                        "success": True,
                        "action": "delivering_to_ws",
                        "shelf_id": next_st.shelf_id,
                        "target_node": next_st.target_node,
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
            # 선반 복귀/포워딩 완료
            shelf_id = current_st.shelf_id

            if st_type == SubTaskType.RETURN_SHELF:
                # 선반 반납 완료
                self.shelf_manager.mark_shelf_returned(shelf_id, current_st.target_node)
                self.robot_manager.set_carrying_shelf(robot.rid, None)

                result = self.task_manager.handle_subtask_complete(task.task_id)

                if result.get("action") == "task_complete":
                    self.robot_manager.complete_task(robot.rid)
                    # 다음 대기 작업 배정 시도
                    self._try_assign_pending_tasks()
                    return {
                        "type": "robot_arrived_ack",
                        "success": True,
                        "action": "task_complete",
                        "task_id": task.task_id,
                    }
                elif result.get("action") == "next_subtask":
                    # 다음 선반으로 이동
                    next_st = task.get_current_subtask()
                    if next_st:
                        self.robot_manager.set_robot_status(robot.rid, RobotStatus.MOVING_TO_SHELF)
                        self._plan_and_publish_move(
                            robot.rid, robot.current_node, next_st.target_node
                        )
                        return {
                            "type": "robot_arrived_ack",
                            "success": True,
                            "action": "moving_to_next_shelf",
                            "target_node": next_st.target_node,
                        }

            elif st_type == SubTaskType.FORWARD_SHELF:
                # 다른 작업대에 도착 → 픽업 대기
                self.shelf_manager.mark_shelf_at_workstation(shelf_id, current_st.target_node)
                result = self.task_manager.handle_subtask_complete(task.task_id)
                next_st = task.get_current_subtask()

                if next_st and next_st.subtask_type == SubTaskType.WAIT_PICKING:
                    self.robot_manager.set_robot_status(robot.rid, RobotStatus.WAITING_FOR_PICK)
                    return {
                        "type": "robot_arrived_ack",
                        "success": True,
                        "action": "wait_picking_at_forward_ws",
                        "shelf_id": shelf_id,
                        "items_to_pick": next_st.items_to_pick,
                    }

        return {"type": "robot_arrived_ack", "success": True, "action": "unknown_state"}

    # ─── 물품 픽업 완료 ───

    def _handle_pick_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        작업자가 물품 픽업 완료 신호

        요청:
        {"type": "pick_complete", "task_id": "T1", "item": "A", "workstation_id": 50}
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
            "worker_marker": 50,
            "shelf_marker": 23
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

    def _plan_and_publish_move(
        self, rid: int, start: int, goal: int
    ) -> Optional[Dict]:
        """로봇 이동 경로 계획 및 MQTT 발행"""
        timed_path = self.path_planner.plan_single_robot(
            start=start,
            goal=goal,
            max_time=self.config.max_time,
        )

        if timed_path is None:
            print(f"[RequestHandler] No path found for Robot {rid}: {start} -> {goal}")
            return None

        mqtt_success = self.mqtt_publisher.publish_single_robot_plan(
            rid=rid,
            start=start,
            goal=goal,
            timed_path=timed_path,
            speed=self.config.default_speed,
        )

        print(f"[RequestHandler] Robot {rid}: planned {start} -> {goal}, "
              f"MQTT={'ok' if mqtt_success else 'fail'}")

        return {
            "rid": rid,
            "start": start,
            "goal": goal,
            "path_length": len(timed_path),
            "mqtt_published": mqtt_success,
        }

    def _error_response(self, message: str) -> Dict[str, Any]:
        """에러 응답"""
        print(f"[RequestHandler] Error: {message}")
        return {"type": "error", "success": False, "error": message}
