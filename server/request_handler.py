"""
요청 처리 모듈

  - JSON 메시지 파싱 + 타입별 라우팅 (handle_message)
  - 시스템/태스크/선반/로봇 상태 조회 핸들러
  - __init__: 상태 변수 + 매니저 보관

실제 로직은 세 mixin에 분리되어 있음 (수정 29, 2026-05-12):
  - MovementMixin: 이동 명령 발행 + 경로 계획 + 충돌/교착 회피 (_movement_mixin.py)
  - MarkerMixin   : AGV 이벤트 (marker, cmd_ack, marker_trigger)        (_marker_mixin.py)
  - WorkflowMixin : 주문/태스크/F-노드/인터셉트 워크플로우                  (_workflow_mixin.py)
"""

import json
import os
from typing import Any, Dict, Optional, List, Set, Tuple

from .config import Config
from .path_planner import PathPlanner
from .mqtt_client import MQTTClient
from .robot_manager import RobotManager, RobotStatus
from .shelf_manager import ShelfManager, ShelfStatus
from .staging_manager import StagingManager, CorridorState
from .task_manager import TaskManager, SubTaskType, TaskStatus
from .db_loader import DBLoader
from .order_optimizer import OrderOptimizer
from ._movement_mixin import MovementMixin
from ._marker_mixin import MarkerMixin
from ._workflow_mixin import WorkflowMixin


class RequestHandler(MovementMixin, MarkerMixin, WorkflowMixin):
    """요청 처리기 — 핸들러 로직은 mixin에서 상속받아 합쳐 동작"""

    # =========================================================================
    # [DEMO MODE] 발표용 단순화 모드
    #   True  → AGV1은 W1(node 33) 전담, AGV2는 W2(node 9) 전담, 스테이징 비활성화
    #   False → 정상 동작 (공정 배정 + 스테이징 활성화)
    # 발표가 끝난 후에는 반드시 False로 되돌릴 것!
    # =========================================================================
    DEMO_MODE = False

    def __init__(
        self,
        config: Config,
        path_planner: PathPlanner,
        mqtt_publisher: MQTTClient,
        robot_manager: RobotManager,
        shelf_manager: ShelfManager,
        staging_manager: StagingManager,
        task_manager: TaskManager,
    ):
        # 매니저 / 유틸 보관
        self.config = config
        self.path_planner = path_planner
        self.mqtt_publisher = mqtt_publisher
        self.robot_manager = robot_manager
        self.shelf_manager = shelf_manager
        self.staging_manager = staging_manager
        self.task_manager = task_manager

        # DB 로더 + 작업 스케줄러
        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Database")
        self.db_loader = DBLoader(db_dir)
        self.task_scheduler = OrderOptimizer(self.db_loader)

        # ─── 상태 변수 (mixin들이 self 통해 공유 접근) ───

        # 포워딩으로 소스 회랑이 미리 해제됐으나 아직 스테이징 노드에 미도착한 로봇
        # rid → target_ws (스테이징 노드 도착 시 이 WS로 이동 명령 발행)
        self._staged_to_ws: Dict[int, int] = {}

        # 포워딩된 선반의 재픽업+반납을 담당하는 로봇
        # shelf_id → rid (pick_complete 후 re-pickup 시 사용)
        self._forwarded_shelf_handlers: Dict[int, int] = {}

        # 명령 전송 대기 중인 로봇 (충돌 예상으로 다음 명령 보류)
        self._blocked_robots: Set[int] = set()

        # Deadlock 해결을 위해 yield_node로 비킨 staging 로봇 (수정 28)
        # corridor 정상 해제 시 staging_node가 아닌 현재(yield) 위치에서 target_ws로 plan 필요
        self._yielded_staging_robots: Set[int] = set()

        # goal 노드가 blocker로 점유된 경우 yield + 대기 (blocker가 goal 떠날 때까지)
        # 무한 deadlock 루프 방지용. blocker 이탈 시 _deferred_goals로 재계획
        self._goal_locked_robots: Set[int] = set()
        self._deferred_goals: Dict[int, int] = {}

        # 이동 중인 로봇의 목적지 노드 예약
        # {node_id: rid} — forward 명령 전송 시 등록, 도착 시 해제
        self._reserved_nodes: Dict[int, int] = {}

        # 브로드캐스트 콜백 (WebSocketHandler에서 설정)
        self._broadcast_callback = None

    def set_broadcast_callback(self, callback):
        """WebSocket 브로드캐스트 콜백 설정"""
        self._broadcast_callback = callback

    async def _broadcast(self, message: dict):
        """브로드캐스트 헬퍼"""
        if self._broadcast_callback:
            await self._broadcast_callback(message)

    # ─── 메시지 라우터 ───

    def handle_message(self, message: str) -> Dict[str, Any]:
        """JSON 메시지 → 타입별 핸들러 디스패치"""
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            return self._error_response(f"Invalid JSON: {e}")

        msg_type = data.get("type")
        if not msg_type:
            return self._error_response("Missing 'type' field")

        handlers = {
            # WorkflowMixin
            "start_order": self._handle_start_order,
            "batch_task_request": self._handle_batch_task,
            "shelf_complete": self._handle_shelf_complete,
            "order_complete": self._handle_order_complete,
            # MarkerMixin
            "marker_report": self._handle_marker_report,
            "cmd_ack": self._handle_cmd_ack,
            # 베이스 (상태 조회)
            "status_request": self._handle_status_request,
            "task_status_request": self._handle_task_status,
            "shelf_status_request": self._handle_shelf_status,
            "robot_status": self._handle_robot_status,
        }

        handler = handlers.get(msg_type)
        if not handler:
            return self._error_response(f"Unknown request type: {msg_type}")

        return handler(data)

    # ─── 상태 조회 핸들러 ───

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

    # ─── 공통 유틸리티 ───

    def _error_response(self, message: str) -> Dict[str, Any]:
        """에러 응답"""
        print(f"[RequestHandler] Error: {message}")
        return {"type": "error", "success": False, "error": message}
