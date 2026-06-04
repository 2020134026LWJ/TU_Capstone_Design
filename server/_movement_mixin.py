"""
RequestHandler — 이동/명령 발행 + 충돌 회피 + 교착 해제 Mixin

이 모듈의 메서드들은 RequestHandler 인스턴스에 mixed-in 되어 동작합니다.
self 상태 접근:
  - self.command_queues         : Dict[int, CommandQueue]  (REFACTOR E: cmd lifecycle, 노드 예약, blocked 추론)
  - self._yielded_staging_robots: Set[int]         (deadlock yield된 staging 로봇)
  - self.robot_manager, self.shelf_manager, self.staging_manager
  - self.path_planner, self.mqtt_publisher, self.config
  - self.DEMO_MODE

다른 mixin 호출:
  - self._handle_marker_report (MarkerMixin)  ← _plan_and_publish_move 즉시 도착 케이스
"""

from typing import Dict, List, Optional, Set, Tuple

from .path_planner import PathPlanner
from .robot_manager import RobotStatus
from .shelf_manager import ShelfStatus
from .command_queue import CommandEntry  # REFACTOR E 2.3


class MovementMixin:
    """이동 명령 발행 + 충돌/교착 회피 로직"""

    # ─── 예약 관리 (REFACTOR E 3.1) ───

    def _clear_robot_reservation(self, rid: int):
        """특정 로봇의 미발행 명령 폐기 (잔여 forward 예약 정리).

        in_flight은 유지 — AGV가 이미 받아서 실행 중이므로 함부로 무효화 불가.
        task complete 직후 idle 전환 + 새 path 계산 전 잔여 정리용.
        """
        queue = self.command_queues.get(rid)
        if queue is not None:
            queue.clear_pending()

    # ─── Blocked / Deadlock 감지 ───

    def _is_staging_robot(self, rid: int) -> bool:
        """Robot이 staging 큐에서 대기 중인지 확인 (corridor.queue 멤버십)

        staging 상태 AGV는 command_queue가 비어있어 _blocked_robots에 등록되지 않음.
        하지만 corridor 진입 경로 위에 정차해 있어 다른 로봇을 막을 수 있으므로
        deadlock 감지/yield 대상으로 포함시켜야 함.
        """
        for corridor in self.staging_manager.corridors.values():
            for staged in corridor.queue:
                if staged.rid == rid:
                    return True
        return False

    def _is_blocked(self, rid: int) -> bool:
        """REFACTOR E 3.3: rid가 보류 상태인지 큐 상태로 추론.

        robot.command_queue에 명령 남았는데 큐 in_flight이 없음 = dispatch 보류 상태.
        (정상 진행 중이면 in_flight 있거나 command_queue 비어있음)
        """
        robot = self.robot_manager.get_robot(rid)
        if not robot or not robot.command_queue:
            return False
        queue = self.command_queues.get(rid)
        return queue is not None and queue.in_flight is None

    def _is_lifting(self, rid: int) -> bool:
        """REFACTOR E 3.4: rid가 리프트 동작 중인지 큐 in_flight cmd로 판단.

        _lifting_robots set 대체. lift cmd_ack 도착 시 큐 ack로 자동 해제.
        """
        queue = self.command_queues.get(rid)
        return (queue is not None
                and queue.in_flight is not None
                and queue.in_flight.cmd in ("lift_up", "lift_down"))

    def _try_dispatch_all(self):
        """REFACTOR E 3.3: 모든 robot dispatch 재시도 + 교착 감지/해제.

        기존 `_retry_blocked_robots`를 큐 기반으로 재작성. `_blocked_robots` set 불필요 —
        `_is_blocked`가 큐 상태로 직접 추론. ACK 도착 시점마다 호출.
        """
        for rid in sorted(self.command_queues.keys()):
            if not self._is_blocked(rid):
                continue
            success = self._send_next_command(rid)
            if not success:
                # 교착 감지: blocker가 또한 blocked / staging / 장기 정차(WAITING_FOR_PICK, IDLE)면 처리 발동
                blocker_rid = self._get_blocker_of(rid)
                blocker = self.robot_manager.get_robot(blocker_rid) if blocker_rid is not None else None
                if blocker_rid is not None and (
                    self._is_blocked(blocker_rid)
                    or self._is_staging_robot(blocker_rid)
                    or (blocker and blocker.status in (RobotStatus.WAITING_FOR_PICK, RobotStatus.IDLE))
                ):
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
            if other.current_node == next_node or self._is_node_reserved_by(next_node, other_rid):
                return other_rid
        return None

    # ─── 큐 기반 노드 예약 조회 (REFACTOR E 3.1) ───

    def _is_node_reserved_by(self, node: int, by_rid: int) -> bool:
        """node가 by_rid의 in_flight 또는 pending head forward 목적지인가?

        _reserved_nodes set/clear 대체. CommandQueue의 peek_expected_node가
        같은 정보를 더 정확히 제공 (마커 도착 시 ack로 자동 clear).
        """
        queue = self.command_queues.get(by_rid)
        return queue is not None and queue.peek_expected_node() == node

    # ─── Yield 노드 탐색 ───

    def _find_yield_node(self, rid: int, contested_node: int) -> Optional[int]:
        """비켜주기용 옆 노드 탐색

        현재 노드의 인접 노드 중:
          - contested_node가 아닐 것
          - 선반 노드가 아닐 것 (물리적 충돌 방지)
          - 다른 로봇이 없을 것
        """
        self._refactor_f_counters['find_yield_node'] += 1
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
                self._is_node_reserved_by(neighbor_id, r_id)
                for r_id in self.robot_manager.robots
                if r_id != rid
            )
            if not occupied and not reserved:
                return neighbor_id

        return None

    # ─── 재계획 ───

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

    # ─── 교착 해제 ───

    def _resolve_deadlock(self, rid_a: int, rid_b: int):
        """교착 해제: 우선순위 낮은 로봇이 비켜줌

        전략 1: 상대 노드를 제외한 우회 경로 계획
        전략 2 (단일 통로): 옆 노드로 한 칸 비켜줌 → 상대방 통과 후 재계획

        우선순위:
          1) staging 상태인 AGV는 항상 yield (이미 대기 중이라 비키는 게 자연스러움)
          2) 선반 운반 중 > 미운반
          3) 동급일 때 max(rid) yield
        """
        self._refactor_f_counters['resolve_deadlock'] += 1
        robot_a = self.robot_manager.get_robot(rid_a)
        robot_b = self.robot_manager.get_robot(rid_b)
        if not robot_a or not robot_b:
            return

        # ─── 진단 로그: deadlock 트리거 시점 양쪽 상태 + corridor dump ───
        a_stg = self._is_staging_robot(rid_a)
        b_stg = self._is_staging_robot(rid_b)
        cdump = ", ".join(
            f"W{ws}={c.state.value}:own={c.occupying_rid}:q={[s.rid for s in c.queue]}"
            for ws, c in self.staging_manager.corridors.items()
        )
        print(f"[Deadlock-DIAG] A={rid_a}(st={robot_a.status.value},"
              f"node={robot_a.current_node},hd={robot_a.heading},"
              f"carry={robot_a.carrying_shelf},stg={a_stg}) "
              f"B={rid_b}(st={robot_b.status.value},"
              f"node={robot_b.current_node},hd={robot_b.heading},"
              f"carry={robot_b.carrying_shelf},stg={b_stg}) "
              f"corridors=[{cdump}]")

        # REFACTOR E 3.4: 리프트 중 yield 차단 (수정 31 계승). _lifting_robots set 대신
        # 큐 in_flight cmd가 lift_*인지로 직접 검사. lift cmd_ack 시 큐 ack가 자동 해제.
        if self._is_lifting(rid_a) or self._is_lifting(rid_b):
            print(f"[RequestHandler] Deadlock 보류: Robot {rid_a}/{rid_b} 중 리프트 동작 중 "
                  f"→ lift 완료 후 재시도")
            return

        # staging 상태는 무조건 yield (carrying 비교 이전)
        a_staging = self._is_staging_robot(rid_a)
        b_staging = self._is_staging_robot(rid_b)
        if a_staging and not b_staging:
            yield_rid, block_rid = rid_a, rid_b
        elif b_staging and not a_staging:
            yield_rid, block_rid = rid_b, rid_a
        else:
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
        if not yield_robot:
            return

        # ─── staging AGV 강제 yield: yield_node로 1-step만 이동 (재계획 안 함) ──
        # corridor 정상 해제 시 yield_node에서 곧장 target_ws로 plan됨 (release 흐름 보정)
        if self._is_staging_robot(yield_rid):
            # 안전 가드: 픽킹 중 AGV는 yield 금지 (픽업자 안전 + 선반 낙하 방지)
            if yield_robot.status in (
                RobotStatus.WAITING_FOR_PICK,
                RobotStatus.PICKING_UP_SHELF,
            ):
                print(f"[RequestHandler] Deadlock: Robot {yield_rid} in "
                      f"{yield_robot.status.value}, yield refused (safety)")
                return
            if yield_rid in self._yielded_staging_robots:
                return  # 이미 yield 명령 발행 — 큐 덮어쓰기 금지 (loop 차단)
            yield_node = self._find_yield_node(yield_rid, block_robot.current_node)
            if yield_node is None:
                print(f"[RequestHandler] Deadlock: staging Robot {yield_rid} cannot yield "
                      f"(no free adjacent node) — stuck")
                return
            commands = self._path_to_commands(
                [yield_robot.current_node, yield_node], yield_robot.heading
            )
            yield_robot.planned_path = [yield_robot.current_node, yield_node]
            yield_robot.command_queue = commands
            self._yielded_staging_robots.add(yield_rid)
            print(f"[RequestHandler] Deadlock (staging yield): Robot {yield_rid} → "
                  f"side {yield_node} (waiting for corridor)")
            self._send_next_command(yield_rid)
            return

        # [REFACTOR F Phase 4.3+4.4] 비-staging 교착은 반응형 복구 없음 — 예방(예약)+대기에 위임.
        #  · head-on: edge 예약(is_edge_free, I2)이 plan 시점 차단 (4.3 — 전략 1/2 제거)
        #  · goal 막힘: 제자리 대기 → blocker 이탈 시 _try_dispatch_all 재시도로 진행 (4.4 — goal-lock 제거)
        return

    # ─── 헬퍼 (선반/대기 노드) ───

    def _get_occupied_shelf_nodes(self) -> Set[int]:
        """현재 선반이 놓여있는 노드 집합 (IN_PLACE 상태인 선반)"""
        occupied = set()
        for shelf in self.shelf_manager.shelves.values():
            if shelf.status == ShelfStatus.IN_PLACE:
                occupied.add(shelf.current_node)
        return occupied

    # ─── 명령 발행 (충돌 체크 포함) ───

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
        """로봇 명령 큐에서 다음 명령 전송 (REFACTOR E 3.2: 큐 단일 진실 원천).

        I1 (큐 단일성): 모든 publish는 이 함수만 호출. queue.dispatch() 직후 publish.
        I3 (no stale): in_flight 점유 중이면 후속 cmd 발행 차단. ack 도착 후 자동 재시도.
        충돌 예상 시 보류 + False 반환 (큐에서 빼지 않음).
        """
        robot = self.robot_manager.get_robot(rid)
        if not robot or not robot.command_queue:
            return False

        # I3: 이전 cmd ack/marker 미수신 → 보류 (AGV _pending_cmd 덮어쓰기 방지)
        # REFACTOR E 3.3: _blocked_robots set 제거 — _is_blocked가 큐 상태로 추론
        queue = self.command_queues.get(rid)
        if queue is not None and queue.in_flight is not None:
            return False

        next_cmd = robot.command_queue[0]
        next_node: Optional[int] = None

        # forward 명령일 때만 충돌 체크
        if next_cmd == "forward":
            next_node = self._get_next_node_by_heading(rid)
            if next_node is None:
                print(f"[RequestHandler] Robot {rid}: blocked → no forward target "
                      f"(heading={robot.heading}°, node={robot.current_node})")
                return False
            for other_rid, other in self.robot_manager.robots.items():
                if other_rid == rid:
                    continue
                occupied = other.current_node == next_node
                reserved = self._is_node_reserved_by(next_node, other_rid)
                if occupied or reserved:
                    reason = "있음" if occupied else "이동중"
                    print(f"[RequestHandler] Robot {rid}: blocked → node {next_node} "
                          f"(AGV-{other_rid} {reason})")
                    return False

        # 큐 entry 생성 + dispatch (in_flight 슬롯 채움)
        if next_cmd == "forward":
            entry = CommandEntry(cmd=next_cmd, target_node=next_node)
        elif next_cmd in ("turn_left", "turn_right", "turn_180"):
            delta = {"turn_right": 90, "turn_left": 270, "turn_180": 180}[next_cmd]
            entry = CommandEntry(
                cmd=next_cmd, expected_heading=(robot.heading + delta) % 360
            )
        else:
            entry = CommandEntry(cmd=next_cmd)
        if queue is not None:
            queue.enqueue(entry)
            queue.dispatch()

        # 명령 전송 (큐 dispatch 후 publish — I1)
        robot.command_queue.pop(0)
        self.mqtt_publisher.publish_cmd(rid, next_cmd)
        return True

    def _predict_heading_after_inflight(self, rid: int) -> Optional[int]:
        """in-flight turn cmd의 효과를 미리 적용한 예측 heading 반환 (REFACTOR E 3.2).

        turn cmd 발행 후 cmd_ack 도착 전에 새 경로를 만들면 robot.heading이 옛 값이라
        path/commands 생성이 stale heading 기준으로 어긋남. CommandEntry.expected_heading
        으로 보정. ack 도착 시 ground truth로 자동 덮어쓰임.
        """
        robot = self.robot_manager.get_robot(rid)
        if not robot:
            return None
        queue = self.command_queues.get(rid)
        if queue is not None and queue.in_flight is not None:
            if queue.in_flight.expected_heading is not None:
                return queue.in_flight.expected_heading
        return robot.heading

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

    # ─── 경로 계획 + 명령 발행 + 스테이징 체크 ───

    def _plan_and_publish_move(
        self, rid: int, start: int, goal: int, is_forwarding: bool = False
    ) -> Optional[Dict]:
        """로봇 이동 경로 계획 → 명령 큐 생성 → 첫 명령 전송

        Args:
            is_forwarding: True면 staging 시 corridor 밖 staging_node 대신
                           진입 경로 위 gateway_node에서 대기 (선반 들고 멀리 우회 방지)
        """
        # Point A: 작업대 스테이징 체크
        # [DEMO MODE] DEMO_MODE=True이면 스테이징 완전 비활성화 → 바로 작업대 진입
        actual_goal = goal
        staging_excluded_node: Optional[int] = None  # staging redirect 시 corridor ws_node 통과 금지
        if not self.DEMO_MODE and goal in self.staging_manager.corridors:
            staging_node = self.staging_manager.should_stage(goal, rid, is_forwarding=is_forwarding)
            if staging_node is not None:
                self._refactor_f_counters['staging_redirect'] += 1
                print(f"[RequestHandler] Robot {rid}: redirected to staging node {staging_node} "
                      f"(target WS {goal})")
                actual_goal = staging_node
                staging_excluded_node = goal
                self.staging_manager.add_staged_agv(goal, rid, staging_node)

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
        # staging redirect: corridor ws_node 통과 금지 (점유 중인 corridor로 지나가서 충돌하는 plan 방지)
        if staging_excluded_node is not None:
            excluded_transit.add(staging_excluded_node)
        # 선반 운반 중이면 IN_PLACE 선반 노드 통과 불가
        if robot and robot.carrying_shelf is not None:
            excluded_transit |= self._get_occupied_shelf_nodes()
        # REFACTOR F Phase 3: self.reservation을 다른 로봇 상태로 snapshot 동기화
        # (Phase 4에서 incremental maintenance로 전환 예정)
        self.reservation.release(rid, fire_callbacks=False)
        for other_rid, other in self.robot_manager.robots.items():
            if other_rid == rid:
                continue
            self.reservation.release(other_rid, fire_callbacks=False)
            # 다른 로봇 planned_path 시공간 예약 (dwell=1: legacy +1 timing 버퍼)
            if other.planned_path:
                self.reservation.commit(other_rid, other.planned_path, dwell=1)
            # 정지 상태 → 영구 장애물. IDLE은 planned_path=[parking_node]가 남을 수 있어 status도 체크.
            # staging 대기 AGV도 영구 장애물 처리 (포워딩 시 gateway-staging 우회 위함).
            if (other.status == RobotStatus.IDLE
                    or not other.planned_path
                    or self._is_staging_robot(other_rid)):
                excluded_transit.add(other.current_node)
            # 다른 로봇이 선반 반납 중이면 그 선반의 원위치 노드도 영구 제외
            if other.status == RobotStatus.RETURNING_SHELF and other.carrying_shelf is not None:
                shelf_obj = self.shelf_manager.shelves.get(other.carrying_shelf)
                if shelf_obj:
                    excluded_transit.add(shelf_obj.home_node)
        if not excluded_transit:
            excluded_transit = None

        # A* 경로 계획 (reservation 기반 시공간 충돌 회피)
        # in-flight turn cmd 효과 반영한 예측 heading 사용 (race 방지)
        planning_heading = self._predict_heading_after_inflight(rid) if robot else None
        timed_path = self.path_planner.astar_with_time(
            start=start,
            goal=actual_goal,
            reservation=self.reservation,
            rid=rid,
            max_time=self.config.max_time,
            excluded_transit=excluded_transit,
            start_heading=planning_heading,
        )

        if timed_path is None:
            print(f"[RequestHandler] No path found for Robot {rid}: {start} -> {actual_goal}")
            return None

        node_path = PathPlanner.compress_to_node_path(timed_path)

        # 명령 큐 생성 및 저장
        if robot:
            robot.planned_path = node_path
            # REFACTOR F Phase 3: 자기 plan reservation 등록 (dwell=1 legacy 호환)
            self.reservation.commit(rid, node_path, dwell=1)
            # commands 생성도 동일한 예측 heading 사용 (A*와 정합)
            robot.command_queue = self._path_to_commands(node_path, planning_heading)
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
