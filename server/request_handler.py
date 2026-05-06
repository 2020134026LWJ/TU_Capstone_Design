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
        db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Database")
        self.db_loader = DBLoader(db_dir)
        self.task_scheduler = TaskScheduler(self.db_loader)

        # 포워딩으로 소스 회랑이 미리 해제됐으나 아직 스테이징 노드에 미도착한 로봇
        # rid → target_ws (스테이징 노드 도착 시 이 WS로 이동 명령 발행)
        self._staged_to_ws: Dict[int, int] = {}

        # 포워딩된 선반의 재픽업+반납을 담당하는 로봇
        # shelf_id → rid (pick_complete 후 re-pickup 시 사용)
        self._forwarded_shelf_handlers: Dict[int, int] = {}

        # 명령 전송 대기 중인 로봇 (충돌 예상으로 다음 명령 보류)
        self._blocked_robots: Set[int] = set()

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
            "marker_report": self._handle_marker_report,
            "cmd_ack": self._handle_cmd_ack,
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
            robot.heading = self._calc_heading(robot.planned_path, node) or robot.heading

        # 위치 기반 회랑 자동 해제
        released = self.staging_manager.check_position_release(rid, node)
        if released is not None:
            released_robot = self.robot_manager.get_robot(released.rid)
            # staged robot이 아직 staging_node에 미도착이면 → 도착 후 처리 (desync 방지)
            if released_robot and released_robot.current_node != released.staging_node:
                self._staged_to_ws[released.rid] = released.target_ws
                print(f"[RequestHandler] Robot {released.rid}: released early (at {released_robot.current_node}), "
                      f"waiting for staging_node {released.staging_node}")
            else:
                start = released_robot.current_node if released_robot else released.staging_node
                self._plan_and_publish_move(released.rid, start, released.target_ws)

        # 포워딩으로 미리 해제된 로봇이 스테이징 노드 도착
        if rid in self._staged_to_ws:
            target_ws = self._staged_to_ws[rid]
            staging_ws = self.staging_manager.get_ws_for_staging_node(node)
            if staging_ws == target_ws:
                del self._staged_to_ws[rid]
                self._plan_and_publish_move(rid, node, target_ws)
                return {"type": "marker_ack", "success": True, "action": "staging_released_proceed"}

        # 스테이징 대기 중인 로봇은 명령 전송 보류
        if self.staging_manager.is_staged_agv(node, rid):
            print(f"[RequestHandler] Robot {rid}: staging at node {node}, holding commands")
            return {"type": "marker_ack", "success": True, "action": "staging_wait"}

        # 스테이징 트리거 마커 체크 (퇴출 중인 로봇만 — RETURNING_SHELF 상태)
        # DELIVERING_TO_WS 상태(입장 경로)에서 trigger 노드를 지날 때는 무시
        released_by_trigger = None
        if robot.status == RobotStatus.RETURNING_SHELF:
            released_by_trigger = self.staging_manager.handle_marker_trigger(rid, node)
        if released_by_trigger:
            released_robot = self.robot_manager.get_robot(released_by_trigger.rid)
            # staged robot이 아직 staging_node에 미도착이면 → 도착 후 처리 (desync 방지)
            if released_robot and released_robot.current_node != released_by_trigger.staging_node:
                self._staged_to_ws[released_by_trigger.rid] = released_by_trigger.target_ws
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

        # 목표 노드가 아닌 중간 노드 → 다음 명령 전송
        self._send_next_command(rid)

        # 블록된 다른 로봇 재시도
        self._retry_blocked_robots()
        self._try_assign_pending_tasks()

        return {"type": "marker_ack", "success": True, "action": "en_route"}

    def _calc_heading(self, planned_path: List[int], current_node: int) -> Optional[int]:
        """경로에서 현재 노드 기준 heading 계산"""
        if not planned_path or current_node not in planned_path:
            return None
        idx = planned_path.index(current_node)
        if idx == 0:
            return None
        prev_node = planned_path[idx - 1]
        px, py = self.path_planner.nodes.get(prev_node, (None, None))
        cx, cy = self.path_planner.nodes.get(current_node, (None, None))
        if px is None or cx is None:
            return None
        dx, dy = cx - px, cy - py
        if abs(dx) < abs(dy):
            return 0 if dy > 0 else 180
        else:
            return 90 if dx > 0 else 270

    def _clear_robot_reservation(self, rid: int):
        """특정 로봇의 노드 예약 모두 해제"""
        to_clear = [n for n, r in self._reserved_nodes.items() if r == rid]
        for n in to_clear:
            del self._reserved_nodes[n]

    def _retry_blocked_robots(self):
        """블록 해제된 로봇들 명령 재시도 + 교착 감지/해제"""
        for rid in sorted(self._blocked_robots.copy()):
            if rid not in self._blocked_robots:
                continue  # 앞 iteration에서 이미 해제됨
            success = self._send_next_command(rid)
            if not success:
                # 교착 감지: 나를 막는 로봇도 나 때문에 blocked 상태이면 우회 경로 계획
                blocker_rid = self._get_blocker_of(rid)
                if blocker_rid is not None and blocker_rid in self._blocked_robots:
                    self._resolve_deadlock(rid, blocker_rid)

    def _get_blocker_of(self, rid: int) -> Optional[int]:
        """rid가 blocked된 원인 로봇 ID 반환 (없으면 None)"""
        robot = self.robot_manager.get_robot(rid)
        if not robot or not robot.command_queue:
            return None
        if robot.command_queue[0] != "forward":
            return None
        next_node = self._get_next_node_by_heading(rid)
        if next_node is None:
            return None
        for other_rid, other in self.robot_manager.robots.items():
            if other_rid == rid:
                continue
            if other.current_node == next_node or self._reserved_nodes.get(next_node) == other_rid:
                return other_rid
        return None

    def _find_yield_node(self, rid: int, contested_node: int) -> Optional[int]:
        """비켜주기용 옆 노드 탐색

        현재 노드의 인접 노드 중:
          - contested_node가 아닐 것
          - 선반 노드가 아닐 것 (물리적 충돌 방지)
          - 다른 로봇이 없을 것
        """
        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return None

        # shelf_manager.all_shelf_nodes 사용 (path_planner.shelf_nodes보다 확실)
        shelf_nodes = self.shelf_manager.all_shelf_nodes

        for neighbor_id, _ in self.path_planner.graph.get(robot.current_node, []):
            if neighbor_id == contested_node:
                continue
            # 선반 노드는 항상 제외 (비켜주기 중간 정차용으로 부적합)
            if neighbor_id in shelf_nodes:
                continue
            # 다른 로봇이 점유/예약 중인 노드 제외
            occupied = any(
                r.current_node == neighbor_id
                for r_id, r in self.robot_manager.robots.items()
                if r_id != rid
            )
            reserved = any(
                self._reserved_nodes.get(neighbor_id) == r_id
                for r_id in self.robot_manager.robots
                if r_id != rid
            )
            if not occupied and not reserved:
                return neighbor_id

        return None

    def _replan_for_placed_shelf(self, placed_node: int):
        """선반이 placed_node에 배치된 후, 해당 노드를 중간 경유하는 운반 로봇 재계획

        선반 배치 전에 계획된 경로가 placed_node를 통과하면
        excluded_transit에 포함되므로 우회 경로로 재계획.
        """
        for rid, robot in self.robot_manager.robots.items():
            if robot.carrying_shelf is None:
                continue
            path = robot.planned_path
            if len(path) < 3:
                continue
            # 중간 경유 노드(start/goal 제외)에 placed_node가 있는지 확인
            if placed_node in path[1:-1]:
                goal = path[-1]
                print(f"[RequestHandler] Shelf placed at {placed_node}: "
                      f"replanning Robot {rid} (was routing through that node)")
                self._plan_and_publish_move(rid, robot.current_node, goal)

    def _resolve_deadlock(self, rid_a: int, rid_b: int):
        """교착 해제: 우선순위 낮은 로봇이 비켜줌

        전략 1: 상대 노드를 제외한 우회 경로 계획
        전략 2 (단일 통로): 옆 노드로 한 칸 비켜줌 → 상대방 통과 후 재계획

        우선순위: 선반 운반 중 > 미운반 ; 같으면 낮은 rid 우선
        """
        robot_a = self.robot_manager.get_robot(rid_a)
        robot_b = self.robot_manager.get_robot(rid_b)
        if not robot_a or not robot_b:
            return

        a_prio = 1 if robot_a.carrying_shelf else 0
        b_prio = 1 if robot_b.carrying_shelf else 0
        if a_prio > b_prio:
            yield_rid, block_rid = rid_b, rid_a
        elif b_prio > a_prio:
            yield_rid, block_rid = rid_a, rid_b
        else:
            yield_rid = max(rid_a, rid_b)
            block_rid = min(rid_a, rid_b)

        yield_robot = self.robot_manager.get_robot(yield_rid)
        block_robot = self.robot_manager.get_robot(block_rid)
        if not yield_robot or not yield_robot.planned_path:
            return

        goal = yield_robot.planned_path[-1]

        # 선반 운반 중이면 IN_PLACE 선반 노드 통과 불가 (두 전략 모두 적용)
        shelf_excluded: Optional[Set[int]] = None
        if yield_robot.carrying_shelf is not None:
            shelf_excluded = self._get_occupied_shelf_nodes()

        # ─── 전략 1: 우회 경로 ─────────────────────────────────────────────
        excluded1: Set[int] = {block_robot.current_node}
        if shelf_excluded:
            excluded1 |= shelf_excluded
        timed_path = self.path_planner.astar_with_time(
            start=yield_robot.current_node,
            goal=goal,
            reserved_nodes=set(),
            reserved_edges=set(),
            max_time=self.config.max_time,
            excluded_transit=excluded1,
            start_heading=yield_robot.heading,
        )
        if timed_path is not None:
            node_path = PathPlanner.compress_to_node_path(timed_path)
            yield_robot.planned_path = node_path
            yield_robot.command_queue = self._path_to_commands(node_path, yield_robot.heading)
            self._blocked_robots.discard(yield_rid)
            print(f"[RequestHandler] Deadlock (alt-path): Robot {yield_rid} → {node_path}")
            if self._send_next_command(yield_rid):
                return
            # alt-path 첫 이동이 여전히 block_robot 위치(= goal)라 즉시 재차단됨
            # → Strategy 2(옆 노드 비켜주기)로 fall-through
            print(f"[RequestHandler] Deadlock (alt-path blocked immediately): "
                  f"Robot {yield_rid} → falling back to yield-node strategy")

        # ─── 전략 2: 옆 노드로 비켜주기 ───────────────────────────────────
        yield_node = self._find_yield_node(yield_rid, block_robot.current_node)
        if yield_node is None:
            print(f"[RequestHandler] Deadlock: Robot {yield_rid} vs Robot {block_rid}, "
                  f"no yield node available — stuck")
            return

        # 비켜준 후 목표까지 전체 경로 재계획
        # yield_node 도착 후 방향은 현재 노드→yield_node 방향
        yield_dir = self.path_planner._node_direction(yield_robot.current_node, yield_node)
        yield_heading = {0: 0, 1: 90, 2: 180, 3: 270}.get(yield_dir)
        timed_path2 = self.path_planner.astar_with_time(
            start=yield_node,
            goal=goal,
            reserved_nodes=set(),
            reserved_edges=set(),
            max_time=self.config.max_time,
            excluded_transit=shelf_excluded,
            start_heading=yield_heading,
        )
        if timed_path2 is None:
            print(f"[RequestHandler] Deadlock: no path from yield_node {yield_node} to goal {goal}")
            return

        path_from_yield = PathPlanner.compress_to_node_path(timed_path2)
        # 전체 경로: 현재 → yield_node → goal
        full_path = [yield_robot.current_node, yield_node] + path_from_yield[1:]
        yield_robot.planned_path = full_path
        yield_robot.command_queue = self._path_to_commands(full_path, yield_robot.heading)
        self._blocked_robots.discard(yield_rid)
        print(f"[RequestHandler] Deadlock (yield): Robot {yield_rid} → side {yield_node} "
              f"→ then {path_from_yield}")
        self._send_next_command(yield_rid)

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

                # lift_up 명령 발행 → AGV 리프트 올림 → cmd_ack 대기
                self.mqtt_publisher.publish_cmd(rid=robot.rid, cmd="lift_up")
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
                # GUI에 AGV 도착 알림 (warehouse/agv/at_ws)
                user_id = int(task.task_id.split("_")[0][1:])
                shelf_label = self.shelf_manager.shelves[next_st.shelf_id].label
                self.mqtt_publisher.client.publish(
                    "warehouse/agv/at_ws",
                    json.dumps({"사용자ID": user_id, "선반번호": shelf_label})
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
            shelf_id = current_st.shelf_id
            self.mqtt_publisher.publish_cmd(rid=robot.rid, cmd="lift_down")
            action = "waiting_shelf_putdown" if st_type == SubTaskType.RETURN_SHELF else "waiting_shelf_putdown_forward"
            return {
                "type": "robot_arrived_ack",
                "success": True,
                "action": action,
                "shelf_id": shelf_id,
            }

        return {"type": "robot_arrived_ack", "success": True, "action": "unknown_state"}

    # ─── 리프트 명령 완료 (cmd_ack) ───

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

        # 회전 완료는 태스크 유무와 무관하게 heading 갱신 + 다음 명령 전송
        # (return-home 등 태스크 없는 이동 중에도 heading을 정확히 유지해야 함)
        if cmd in ("turn_left", "turn_right", "turn_180"):
            if cmd == "turn_right":
                robot.heading = (robot.heading + 90) % 360
            elif cmd == "turn_left":
                robot.heading = (robot.heading + 270) % 360
            elif cmd == "turn_180":
                robot.heading = (robot.heading + 180) % 360
            print(f"[RequestHandler] Robot {robot.rid}: heading updated to {robot.heading}° after {cmd}")
            self._send_next_command(robot.rid)
            self._retry_blocked_robots()
            return {"type": "cmd_ack_response", "success": True, "action": f"turned_{cmd}"}

        task_id = robot.current_task_id
        task = self.task_manager.get_task(task_id) if task_id else None
        if not task:
            return {"type": "cmd_ack_response", "success": True, "action": "no_task"}

        current_st = task.get_current_subtask()
        if not current_st:
            return {"type": "cmd_ack_response", "success": True, "action": "no_subtask"}

        if cmd == "lift_up":
            return self._handle_pickup_ack(robot, task, current_st)
        elif cmd == "lift_down":
            return self._handle_putdown_ack(robot, task, current_st)

        return {"type": "cmd_ack_response", "success": True, "action": "unknown_cmd"}

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

        # user_id → WS 노드: robot home 기준 (포워딩 시 해당 user의 task가 없을 수 있음)
        robot = self.robot_manager.get_robot(user_id)
        if not robot:
            return self._error_response(f"Unknown user {user_id}")

        ws_node = robot.home_node

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
                self._plan_and_publish_move(robot.rid, source_ws, forward_ws)
                return {"success": True, "action": "forwarding_shelf", "forward_to_ws": forward_ws}

        elif result.get("action") == "shelf_done_pickup_for_return":
            # 포워딩된 선반 픽업 완료 → 포워딩 로봇이 re-pickup 후 반납
            shelf_id_r = result.get("shelf_id")
            robot = self.robot_manager.get_robot(task.assigned_robot) if task else None

            if robot and shelf_id_r:
                self.shelf_manager.mark_shelf_picked_up(shelf_id_r, robot.rid)
                self.robot_manager.set_carrying_shelf(robot.rid, shelf_id_r)
                self.robot_manager.set_robot_status(robot.rid, RobotStatus.PICKING_UP_SHELF)
                self.mqtt_publisher.publish_cmd(rid=robot.rid, cmd="lift_up")
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

    def _get_occupied_shelf_nodes(self) -> Set[int]:
        """현재 선반이 놓여있는 노드 집합 (IN_PLACE 상태인 선반)"""
        occupied = set()
        for shelf in self.shelf_manager.shelves.values():
            if shelf.status == ShelfStatus.IN_PLACE:
                occupied.add(shelf.current_node)
        return occupied

    def _get_idle_wait_node(self, rid: int) -> int:
        """idle 로봇의 대기 노드: 홈 WS의 staging 노드 반환 (staging 없으면 home_node)"""
        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return None
        corridor = self.staging_manager.corridors.get(robot.home_node)
        if corridor:
            return corridor.staging_node
        return robot.home_node

    def _path_to_commands(self, node_path: List[int], start_heading: int) -> List[str]:
        """노드 경로를 이동 명령 리스트로 변환

        Args:
            node_path: 노드 ID 리스트 [현재노드, 다음노드, ...]
            start_heading: 출발 시 heading (0=북, 90=동, 180=남, 270=서)

        Returns:
            명령 리스트 ["turn_left", "forward", "forward", ...]
        """
        # 방향 계산: 노드 좌표 기반
        DIR_NORTH = 0
        DIR_EAST  = 90
        DIR_SOUTH = 180
        DIR_WEST  = 270

        def node_direction(from_node: int, to_node: int) -> Optional[int]:
            fx, fy = self.path_planner.nodes.get(from_node, (0, 0))
            tx, ty = self.path_planner.nodes.get(to_node, (0, 0))
            dx, dy = tx - fx, ty - fy
            if abs(dx) < abs(dy):
                return DIR_NORTH if dy > 0 else DIR_SOUTH
            else:
                return DIR_EAST if dx > 0 else DIR_WEST

        def turn_commands(current_h: int, target_h: int) -> List[str]:
            diff = (target_h - current_h) % 360
            if diff == 0:
                return []
            elif diff == 90:
                return ["turn_right"]
            elif diff == 180:
                return ["turn_180"]
            else:  # diff == 270
                return ["turn_left"]

        commands = []
        heading = start_heading

        for i in range(1, len(node_path)):
            target_dir = node_direction(node_path[i - 1], node_path[i])
            if target_dir is None:
                continue
            commands.extend(turn_commands(heading, target_dir))
            commands.append("forward")
            heading = target_dir

        return commands

    def _send_next_command(self, rid: int) -> bool:
        """로봇 명령 큐에서 다음 명령 전송

        충돌이 예상되면 명령을 보류하고 False 반환.
        """
        robot = self.robot_manager.get_robot(rid)
        if not robot or not robot.command_queue:
            return False

        next_cmd = robot.command_queue[0]

        # forward 명령일 때만 충돌 체크
        if next_cmd == "forward":
            # 현재 노드에서 heading 방향의 다음 노드 계산
            next_node = self._get_next_node_by_heading(rid)
            if next_node is None:
                # heading 방향 노드 없음 → 안전하게 차단 (체크 없이 보내면 충돌 위험)
                self._blocked_robots.add(rid)
                print(f"[RequestHandler] Robot {rid}: blocked → no forward target "
                      f"(heading={robot.heading}°, node={robot.current_node})")
                return False
            # 다른 로봇이 next_node에 있거나 이동 중(예약)인 경우 보류
            for other_rid, other in self.robot_manager.robots.items():
                if other_rid == rid:
                    continue
                occupied = other.current_node == next_node
                reserved = self._reserved_nodes.get(next_node) == other_rid
                if occupied or reserved:
                    self._blocked_robots.add(rid)
                    reason = "있음" if occupied else "이동중"
                    print(f"[RequestHandler] Robot {rid}: blocked → node {next_node} "
                          f"(AGV-{other_rid} {reason})")
                    return False
            # 충돌 없음 → 목적지 노드 예약
            self._reserved_nodes[next_node] = rid

        # 명령 전송
        robot.command_queue.pop(0)
        self._blocked_robots.discard(rid)
        self.mqtt_publisher.publish_cmd(rid, next_cmd)
        return True

    def _get_next_node_by_heading(self, rid: int) -> Optional[int]:
        """현재 heading 방향으로 한 칸 이동 시 도착할 노드"""
        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return None

        cx, cy = self.path_planner.nodes.get(robot.current_node, (None, None))
        if cx is None:
            return None

        # heading 방향의 이웃 노드 탐색
        heading = robot.heading
        for neighbor_id, _ in self.path_planner.graph.get(robot.current_node, []):
            nx, ny = self.path_planner.nodes.get(neighbor_id, (None, None))
            if nx is None:
                continue
            dx, dy = nx - cx, ny - cy
            if heading == 0   and abs(dx) < abs(dy) and dy > 0:
                return neighbor_id
            if heading == 180 and abs(dx) < abs(dy) and dy < 0:
                return neighbor_id
            if heading == 90  and abs(dy) < abs(dx) and dx > 0:
                return neighbor_id
            if heading == 270 and abs(dy) < abs(dx) and dx < 0:
                return neighbor_id
        return None

    def _plan_and_publish_move(
        self, rid: int, start: int, goal: int
    ) -> Optional[Dict]:
        """로봇 이동 경로 계획 → 명령 큐 생성 → 첫 명령 전송"""
        # Point A: 작업대 스테이징 체크
        # [DEMO MODE] DEMO_MODE=True이면 스테이징 완전 비활성화 → 바로 작업대 진입
        actual_goal = goal
        if not self.DEMO_MODE and goal in self.staging_manager.corridors:
            staging_node = self.staging_manager.should_stage(goal, rid)
            if staging_node is not None:
                actual_goal = staging_node
                self.staging_manager.add_staged_agv(goal, rid, staging_node)
                print(f"[RequestHandler] Robot {rid}: redirected to staging node {staging_node} "
                      f"(target WS {goal})")

        # 이미 목적지에 있으면 즉시 도착 처리
        if start == actual_goal:
            print(f"[RequestHandler] Robot {rid}: already at goal {actual_goal}, immediate arrival")
            self._handle_marker_report({
                "type": "marker_report", "rid": rid, "marker_id": actual_goal,
            })
            return {"rid": rid, "start": start, "goal": actual_goal, "path_length": 0}

        # 타임아웃 해제 대기 AGV 처리
        if self.staging_manager.pending_timeout_releases:
            timeout_releases = list(self.staging_manager.pending_timeout_releases)
            self.staging_manager.pending_timeout_releases.clear()
            for t_released in timeout_releases:
                t_robot = self.robot_manager.get_robot(t_released.rid)
                if t_robot:
                    self._plan_and_publish_move(
                        t_released.rid, t_robot.current_node, t_released.target_ws
                    )

        # 경로 계획 시 통과 불가 노드 수집
        robot = self.robot_manager.get_robot(rid)
        excluded_transit: Set[int] = set()
        # 선반 운반 중이면 IN_PLACE 선반 노드 통과 불가
        if robot and robot.carrying_shelf is not None:
            excluded_transit |= self._get_occupied_shelf_nodes()
        # 다른 로봇의 planned_path 기반 시간 예약 (Cooperative A*)
        reserved_nodes: Set[Tuple[int, int]] = set()
        for other_rid, other in self.robot_manager.robots.items():
            if other_rid == rid:
                continue
            # 다른 로봇의 planned_path 각 노드를 도착 예상 시각으로 예약
            for t, node in enumerate(other.planned_path):
                reserved_nodes.add((node, t))
                reserved_nodes.add((node, t + 1))  # 1스텝 버퍼 (회전 지연 보정)
            # planned_path 없으면 현재 위치를 t=0~2로 예약
            if not other.planned_path:
                for t in range(3):
                    reserved_nodes.add((other.current_node, t))
            # 다른 로봇이 선반 반납 중이면 그 선반의 원위치 노드도 영구 제외
            if other.status == RobotStatus.RETURNING_SHELF and other.carrying_shelf is not None:
                shelf_obj = self.shelf_manager.shelves.get(other.carrying_shelf)
                if shelf_obj:
                    excluded_transit.add(shelf_obj.home_node)
        if not excluded_transit:
            excluded_transit = None

        # A* 경로 계획 (시간 기반 예약으로 상대 경로와 충돌 없는 최적 경로 계획)
        timed_path = self.path_planner.astar_with_time(
            start=start,
            goal=actual_goal,
            reserved_nodes=reserved_nodes,
            reserved_edges=set(),
            max_time=self.config.max_time,
            excluded_transit=excluded_transit,
            start_heading=robot.heading if robot else None,
        )

        if timed_path is None:
            print(f"[RequestHandler] No path found for Robot {rid}: {start} -> {actual_goal}")
            return None

        node_path = PathPlanner.compress_to_node_path(timed_path)

        # 명령 큐 생성 및 저장
        if robot:
            robot.planned_path = node_path
            robot.command_queue = self._path_to_commands(node_path, robot.heading)
            print(f"[RequestHandler] Robot {rid}: path={node_path}, "
                  f"commands={robot.command_queue}")
            # 첫 명령 전송
            self._send_next_command(rid)

        return {
            "rid": rid,
            "start": start,
            "goal": actual_goal,
            "original_goal": goal,
            "path_length": len(node_path),
        }

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

    def _error_response(self, message: str) -> Dict[str, Any]:
        """에러 응답"""
        print(f"[RequestHandler] Error: {message}")
        return {"type": "error", "success": False, "error": message}
