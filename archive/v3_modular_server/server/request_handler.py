"""
요청 처리 모듈
- JSON 파싱 및 검증
- 요청 타입별 라우팅
- 에러 처리
"""

import json
import time
from typing import Any, Dict, Optional, Tuple

from .config import Config
from .path_planner import PathPlanner
from .mqtt_publisher import MQTTPublisher
from .robot_manager import RobotManager, RobotStatus


class RequestHandler:
    """요청 처리기"""

    def __init__(
        self,
        config: Config,
        path_planner: PathPlanner,
        mqtt_publisher: MQTTPublisher,
        robot_manager: RobotManager
    ):
        self.config = config
        self.path_planner = path_planner
        self.mqtt_publisher = mqtt_publisher
        self.robot_manager = robot_manager

    def handle_message(self, message: str) -> Dict[str, Any]:
        """
        메시지 처리

        Args:
            message: JSON 문자열

        Returns:
            응답 딕셔너리
        """
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            return self._error_response(f"Invalid JSON: {e}")

        msg_type = data.get("type")
        if not msg_type:
            return self._error_response("Missing 'type' field")

        # 요청 타입별 라우팅
        handlers = {
            "task_request": self._handle_task_request,
            "status_request": self._handle_status_request,
            "robot_status": self._handle_robot_status,
        }

        handler = handlers.get(msg_type)
        if not handler:
            return self._error_response(f"Unknown request type: {msg_type}")

        return handler(data)

    def _handle_task_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        작업 요청 처리

        요청 형식:
        {
            "type": "task_request",
            "worker_id": 1,
            "worker_marker": 37,
            "shelf_marker": 23
        }
        """
        # 필수 필드 검증
        worker_id = data.get("worker_id")
        worker_marker = data.get("worker_marker")
        shelf_marker = data.get("shelf_marker")

        if worker_id is None:
            return self._error_response("Missing 'worker_id'")
        if worker_marker is None:
            return self._error_response("Missing 'worker_marker'")
        if shelf_marker is None:
            return self._error_response("Missing 'shelf_marker'")

        # 마커 → 노드 변환 (1:1 매핑)
        start_node = int(worker_marker)
        goal_node = int(shelf_marker)

        # 노드 유효성 검사
        if not self.path_planner.is_valid_node(start_node):
            return self._error_response(f"Invalid worker_marker: {worker_marker}")
        if not self.path_planner.is_valid_node(goal_node):
            return self._error_response(f"Invalid shelf_marker: {shelf_marker}")

        # 로봇 조회 (worker_id = robot_id)
        robot = self.robot_manager.get_robot_by_worker(worker_id)
        if not robot:
            return self._error_response(f"Robot not found for worker_id: {worker_id}")

        # 로봇 현재 위치 업데이트
        self.robot_manager.update_robot_position(robot.rid, start_node)

        # 경로 계획
        timed_path = self.path_planner.plan_single_robot(
            start=start_node,
            goal=goal_node,
            max_time=self.config.max_time
        )

        if timed_path is None:
            return self._error_response(f"No path found: {start_node} -> {goal_node}")

        # 노드 경로 압축
        node_path = PathPlanner.compress_to_node_path(timed_path)

        # 작업 정보
        task = {
            "worker_id": worker_id,
            "start_node": start_node,
            "goal_node": goal_node,
            "node_path": node_path,
            "timestamp": time.time()
        }

        # 로봇에 작업 할당
        self.robot_manager.assign_task(robot.rid, task)

        # MQTT 발행
        robots_payload = [{
            "rid": robot.rid,
            "start": start_node,
            "goal": goal_node,
            "node_path": node_path,
            "timed_path": [{"node": n, "t": t} for (n, t) in timed_path]
        }]

        mqtt_success = self.mqtt_publisher.publish_plan(
            robots=robots_payload,
            speed=self.config.default_speed
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
            "mqtt_published": mqtt_success
        }

    def _handle_status_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """상태 요청 처리"""
        return {
            "type": "status_response",
            "success": True,
            "mqtt_connected": self.mqtt_publisher.is_connected(),
            "robots": self.robot_manager.get_status_summary()
        }

    def _handle_robot_status(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """로봇 상태 업데이트 (bridge.py에서 수신)"""
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

        return {
            "type": "robot_status_ack",
            "success": True
        }

    def _error_response(self, message: str) -> Dict[str, Any]:
        """에러 응답 생성"""
        print(f"[RequestHandler] Error: {message}")
        return {
            "type": "error",
            "success": False,
            "error": message
        }
