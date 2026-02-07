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
        task_manager: TaskManager,
    ):
        self.config = config
        self.path_planner = path_planner
        self.mqtt_publisher = mqtt_publisher
        self.robot_manager = robot_manager
        self.shelf_manager = shelf_manager
        self.task_manager = task_manager

        # 엑셀 DB 로더 및 작업 스케줄러
        import os
        db_dir = os.path.join(config.base_dir, "Database")
        self.db_loader = DBLoader(db_dir)
        self.task_scheduler = TaskScheduler(self.db_loader)

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
            # 새로운 주문 처리 메시지
            "start_order": self._handle_start_order,
            "shelf_complete": self._handle_shelf_complete,
            "order_complete": self._handle_order_complete,
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
        """
        대기 중인 작업에 유휴 로봇 할당 시도
        - 모든 유휴 로봇에 작업 배정
        - prioritized_planning으로 동시에 경로 계획 (충돌 회피)
        """
        assignments = []
        robots_to_move = []  # [(rid, start, goal, task_id), ...]

        # 1단계: 모든 유휴 로봇에 대기 작업 배정
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

            # 이동할 로봇 목록에 추가
            robots_to_move.append({
                "rid": robot.rid,
                "start": robot.current_node,
                "goal": first_st.target_node,
                "task_id": task.task_id,
            })

            assignments.append({
                "task_id": task.task_id,
                "robot_id": robot.rid,
                "first_target": first_st.target_node,
                "path_planned": False,  # 아래에서 업데이트
            })

        # 2단계: 여러 로봇이 동시에 움직일 경우 prioritized_planning 사용
        if len(robots_to_move) > 0:
            move_result = self._plan_and_publish_multi_robot_move(robots_to_move)
            # 결과 업데이트
            for i, assignment in enumerate(assignments):
                if i < len(move_result):
                    assignment["path_planned"] = move_result[i].get("success", False)

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

    # ─── 새로운 주문 처리 (start_order, shelf_complete, order_complete) ───

    def _handle_start_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        주문 시작 처리 (엑셀 DB → TaskScheduler 최적화 → AGV 경로 전송)

        요청:
        {"type": "start_order", "사용자ID": 1, "주문번호": 1}
        """
        user_id = data.get("사용자ID")
        order_id = data.get("주문번호")

        if user_id is None or order_id is None:
            return self._error_response("Missing '사용자ID' or '주문번호'")

        # TaskScheduler로 최적화된 스케줄 생성
        schedule = self.task_scheduler.schedule_order(
            user_id=user_id,
            order_id=order_id,
            optimization="nearest",
        )

        if not schedule:
            return self._error_response(f"Order {order_id} not found for user {user_id}")

        self.task_scheduler.print_schedule(schedule)

        task_id = f"ORDER_{user_id}_{order_id}"
        workstation_id = schedule["workstation"]
        optimized_shelf_sequence = schedule["shelf_sequence"]

        # 최적화된 순서에 맞게 물품 목록 재배열
        optimized_items = []
        for task in schedule["tasks"]:
            optimized_items.extend(task.items)

        print(f"[RequestHandler] Optimized order: {optimized_items}")
        print(f"[RequestHandler] Shelf sequence: {optimized_shelf_sequence}")

        # 내부적으로 batch_task 생성 (최적화된 선반 순서 포함)
        batch_data = {
            "type": "batch_task_request",
            "tasks": [
                {
                    "task_id": task_id,
                    "workstation_id": workstation_id,
                    "items": optimized_items,
                    "optimized_shelf_sequence": optimized_shelf_sequence,
                }
            ]
        }

        result = self._handle_batch_task(batch_data)

        return {
            "type": "start_order_response",
            "success": result.get("success", False),
            "사용자ID": user_id,
            "주문번호": order_id,
            "task_id": task_id,
            "items": optimized_items,
            "shelf_sequence": optimized_shelf_sequence,
            "total_shelves": schedule["total_shelves"],
            "message": f"주문 {order_id} 작업 시작 (최적화된 경로: {len(optimized_shelf_sequence)}개 선반)",
        }

    def _get_order_from_db(self, user_id: int, order_id: int) -> Optional[Dict]:
        """
        엑셀 DB에서 주문 정보 조회

        반환 형식: {"workstation_id": 50, "items": ["드롭스", "퍼지", ...]}
        """
        order_info = self.db_loader.get_order(user_id, order_id)
        if not order_info:
            return None

        # items: [{"name": "드롭스", "quantity": 3}, ...] → ["드롭스", "드롭스", "드롭스", ...]
        # 또는 단순히 물품명 리스트로 변환 (quantity는 나중에 처리)
        items = []
        for item in order_info["items"]:
            # 수량만큼 반복하지 않고, 물품명만 추가 (선반 방문 최적화 위해)
            items.append(item["name"])

        return {
            "workstation_id": order_info["workstation_id"],
            "items": items,
        }

    def _handle_shelf_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        선반/서랍 물품 픽업 완료

        요청:
        {"type": "shelf_complete", "사용자ID": 1, "선반번호": "1-1"}

        선반번호 형식: "선반ID-서랍번호" (예: "1-1" = 선반1의 1번 서랍)
        """
        user_id = data.get("사용자ID")
        shelf_drawer = data.get("선반번호")

        if user_id is None or shelf_drawer is None:
            return self._error_response("Missing '사용자ID' or '선반번호'")

        # 선반번호 파싱: "1-1" → shelf_id=1, drawer=1
        try:
            parts = shelf_drawer.split("-")
            shelf_id = int(parts[0])
            drawer_num = int(parts[1]) if len(parts) > 1 else 1
        except (ValueError, IndexError):
            return self._error_response(f"Invalid 선반번호 format: {shelf_drawer}")

        # 해당 사용자의 현재 진행 중인 작업 찾기
        task_id = self._find_active_task_for_user(user_id)
        if not task_id:
            return self._error_response(f"No active task for user {user_id}")

        # 선반-서랍 → 물품 매핑 (TODO: 실제 매핑 로직)
        item = self._get_item_from_shelf_drawer(shelf_id, drawer_num)

        # pick_complete 로직 호출
        pick_data = {
            "type": "pick_complete",
            "task_id": task_id,
            "item": item,
        }

        result = self._handle_pick_complete(pick_data)

        return {
            "type": "shelf_complete_response",
            "success": result.get("success", False),
            "사용자ID": user_id,
            "선반번호": shelf_drawer,
            "item": item,
            "action": result.get("action"),
            "remaining_items": result.get("remaining_items_on_shelf", []),
        }

    def _find_active_task_for_user(self, user_id: int) -> Optional[str]:
        """사용자의 현재 진행 중인 작업 ID 찾기"""
        # 작업 ID 형식: ORDER_{user_id}_{order_id}
        prefix = f"ORDER_{user_id}_"
        for task in self.task_manager.tasks.values():
            if task.task_id.startswith(prefix) and task.status == TaskStatus.IN_PROGRESS:
                return task.task_id
        return None

    def _get_item_from_shelf_drawer(self, shelf_id: int, drawer_num: int) -> str:
        """
        선반-서랍 → 물품명 매핑 (stub)

        TODO: 실제 재고 DB에서 조회
        """
        # 임시 매핑 - 선반 노드 ID와 서랍 번호로 물품명 생성
        # 실제로는 shelf_config.json 또는 DB에서 조회해야 함
        shelf_node_map = {1: 9, 2: 11, 3: 13, 4: 23, 5: 25, 6: 27, 7: 37, 8: 39, 9: 41}
        shelf_node = shelf_node_map.get(shelf_id, 9)

        shelf = self.shelf_manager.get_shelf(shelf_node)
        if shelf and drawer_num <= len(shelf.items):
            return shelf.items[drawer_num - 1]

        # fallback: 기본 물품명
        return f"ITEM_{shelf_id}_{drawer_num}"

    def _handle_order_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        주문 완료 확인

        요청:
        {"type": "order_complete", "사용자ID": 1, "주문번호": 1}
        """
        user_id = data.get("사용자ID")
        order_id = data.get("주문번호")

        if user_id is None or order_id is None:
            return self._error_response("Missing '사용자ID' or '주문번호'")

        task_id = f"ORDER_{user_id}_{order_id}"
        task = self.task_manager.get_task(task_id)

        if not task:
            return self._error_response(f"Task {task_id} not found")

        # 작업 상태 확인
        is_complete = task.status == TaskStatus.COMPLETED
        remaining_items = []

        if not is_complete:
            # 남은 물품 목록
            current_st = task.get_current_subtask()
            if current_st and current_st.items_to_pick:
                remaining_items = current_st.items_to_pick

        return {
            "type": "order_complete_response",
            "success": True,
            "사용자ID": user_id,
            "주문번호": order_id,
            "task_id": task_id,
            "is_complete": is_complete,
            "status": task.status.value,
            "remaining_items": remaining_items,
            "message": "주문 완료" if is_complete else "주문 진행 중",
        }

    # ─── 유틸리티 ───

    def _plan_and_publish_multi_robot_move(
        self, robots_to_move: List[Dict]
    ) -> List[Dict]:
        """
        다중 로봇 경로 계획 및 MQTT 발행 (prioritized planning)

        Args:
            robots_to_move: [{"rid": 1, "start": 50, "goal": 9, "task_id": "T1"}, ...]

        Returns:
            [{"rid": 1, "success": True, "path_length": 5}, ...]
        """
        if not robots_to_move:
            return []

        # 단일 로봇이면 기존 방식 사용
        if len(robots_to_move) == 1:
            r = robots_to_move[0]
            result = self._plan_and_publish_move(r["rid"], r["start"], r["goal"])
            return [{"rid": r["rid"], "success": result is not None}]

        # 다중 로봇: prioritized_planning 사용
        starts = [r["start"] for r in robots_to_move]
        goals = [r["goal"] for r in robots_to_move]

        print(f"[RequestHandler] Multi-robot planning: {len(robots_to_move)} robots")
        for r in robots_to_move:
            print(f"  Robot {r['rid']}: {r['start']} -> {r['goal']}")

        # DEBUG: 단일 로봇 경로 테스트
        for r in robots_to_move:
            test_path = self.path_planner.plan_single_robot(r['start'], r['goal'])
            if test_path:
                from .path_planner import PathPlanner
                node_path = PathPlanner.compress_to_node_path(test_path)
                print(f"  [DEBUG] Robot {r['rid']} path: {node_path}")

        paths = self.path_planner.prioritized_planning(
            starts=starts,
            goals=goals,
            max_time=self.config.max_time,
            stay_time_at_goal=self.config.stay_time_at_goal,
        )

        if paths is None:
            print("[RequestHandler] Multi-robot planning failed!")
            return [{"rid": r["rid"], "success": False} for r in robots_to_move]

        # MQTT로 모든 로봇 경로 동시 발행
        from .path_planner import PathPlanner

        mqtt_robots = []
        for i, r in enumerate(robots_to_move):
            if i < len(paths) and paths[i]:
                timed_path = paths[i]
                node_path = PathPlanner.compress_to_node_path(timed_path)
                mqtt_robots.append({
                    "rid": r["rid"],
                    "start": r["start"],
                    "goal": r["goal"],
                    "node_path": node_path,
                    "timed_path": [{"node": n, "t": t} for (n, t) in timed_path],
                })

        mqtt_success = self.mqtt_publisher.publish_plan(
            robots=mqtt_robots,
            speed=self.config.default_speed,
        )

        print(f"[RequestHandler] Multi-robot plan published: {len(mqtt_robots)} robots, "
              f"MQTT={'ok' if mqtt_success else 'fail'}")

        results = []
        for i, r in enumerate(robots_to_move):
            success = i < len(paths) and paths[i] is not None
            results.append({
                "rid": r["rid"],
                "success": success,
                "path_length": len(paths[i]) if success else 0,
            })
        return results

    def _plan_and_publish_move(
        self, rid: int, start: int, goal: int
    ) -> Optional[Dict]:
        """단일 로봇 이동 경로 계획 및 MQTT 발행"""
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
