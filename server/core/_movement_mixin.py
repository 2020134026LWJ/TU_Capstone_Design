"""
RequestHandler — 이동/명령 발행 + 충돌 회피(예방형) Mixin

이 모듈의 메서드들은 RequestHandler 인스턴스에 mixed-in 되어 동작합니다.
교착은 plan 시점 예약(I1/I2) + staging_node transit 제외(4.5.6)로 *예방* — 반응형 해제 없음.
self 상태 접근:
  - self.command_queues         : Dict[int, CommandQueue]  (REFACTOR E: cmd lifecycle, 노드 예약, blocked 추론)
  - self.robot_manager, self.shelf_manager, self.staging_manager
  - self.path_planner, self.mqtt_publisher, self.config
  - self.DEMO_MODE

다른 mixin 호출:
  - self._handle_marker_report (MarkerMixin)  ← _plan_and_publish_move 즉시 도착 케이스

섹션 맵 (메서드 → 역할):
  ── 핵심 발행 ──
  _plan_and_publish_move     [핵심] should_stage(STG) → A*(reservation) → cmd 큐 생성 → 발행
                             ★ staging_node transit 제외(4.5.6) = staging 교착 예방
                             ★ B-selfguard: in-flight면 _pending_replan 보류 후 return
                               (계획은 in_flight None일 때만 — stale 위치 계획 구조적 차단)
  _send_next_command         큐 head cmd 발행 (forward 전 충돌 체크 포함)
  _try_dispatch_all          blocked 로봇 재시도 (blocker 이탈 시 진행 — 반응형 해제 없음)
  ── 경로 / 명령 변환 ──
  _path_to_commands          노드 경로 → forward/turn 명령 리스트 (turn 최소화)
  _get_next_node_by_heading  현재 heading 기준 다음 노드
  _flush_pending_replan      B-selfguard: 보류된 재계획을 fresh 시점(마커/ack)에 실행
  _replan_for_placed_shelf   선반 배치 후 그 노드 경유 로봇 재계획
  ── 큐/예약/상태 추론 (REFACTOR E) ──
  _is_blocked                cmd 남았는데 in_flight 없음 = dispatch 보류 추론
  _is_lifting                in_flight cmd가 lift_* 인지
  _is_node_reserved_by       노드가 특정 로봇의 예약 목적지인지 (peek_expected_node)
  _is_staging_robot          corridor 큐 멤버십 (excluded_transit 영구장애물용)
  _clear_robot_reservation   로봇 예약 해제
  _get_occupied_shelf_nodes  IN_PLACE 선반 노드 집합 (운반 시 통과 금지)
"""

import os
from typing import Dict, List, Optional, Set, Tuple

from ..planning.path_planner import PathPlanner
from ..planning.deadlock_detector import find_wait_cycle  # 수정 54: 교착 감지 도구
from ..managers.robot import RobotStatus
from ..managers.shelf import ShelfStatus
from ..planning.command_queue import CommandEntry  # REFACTOR E 2.3

# 수정 82: 안전망('내려놓고 돌기') 토글. 기본 ON. SAFETY_NET=0 이면 끔(오라클 대조검증용).
SAFETY_NET_ENABLED = os.environ.get("SAFETY_NET", "1") == "1"

# 수정 84: 든로봇(움직이는) 위험엔 내려놓기 전에 잠깐 '대기'(상대가 곧 비킴). 정적선반은
# 대기 불가(안 비킴)→즉시 내려놓기. 대칭교착 방지로 N회 초과 시 폴백=내려놓고 돌기.
TURN_DEFER_MAX = int(os.environ.get("TURN_DEFER_MAX", "3"))


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
        """REFACTOR E 3.3 / F 4.5.6: blocked robot dispatch 재시도. ACK 도착 시점마다 호출.

        blocker가 떠나면 다음 호출에서 진행. 교착은 plan 시점 예약(I1/I2) +
        staging_node transit 제외(4.5.6 Step 1)로 *예방*되므로 대부분 반응형 해제 불필요.
        예외(수정 54): 예약의 lockstep 가정이 비동기 실행(회전=실시간 추가)으로 깨질 때
        예방이 못 잡는 교착이 남는다 → 매 주행마다 wait-for 사이클을 감지해 한쪽을 우회.
        """
        for rid in sorted(self.command_queues.keys()):
            if self._is_blocked(rid):
                self._send_next_command(rid)
        # 수정 54: 재시도 후에도 남은 wait-for 사이클(일반 교착)이면 backstop 해소
        cycle = self._detect_deadlock_cycle()
        if cycle is not None:
            self._resolve_deadlock(cycle)

    def _robot_at(self, node: Optional[int]) -> Optional[int]:
        """node에 현재 정지/위치한 로봇 rid (없으면 None)."""
        if node is None:
            return None
        for rid, robot in self.robot_manager.robots.items():
            if robot.current_node == node:
                return rid
        return None

    def _detect_deadlock_cycle(self) -> Optional[List[int]]:
        """일반 교착(wait-for 사이클) 감지 (수정 54).

        로봇 상태에서 wait_for 맵(rid → 가려는 노드를 점유한 상대)을 만들고,
        순수 사이클 찾기는 planning.deadlock_detector.find_wait_cycle에 위임
        (layering: 감지=도구, 해소=core). 막힌(_is_blocked) + 다음 cmd가 forward인
        로봇만 대상 — turn 대기 중이면 아직 노드 점유 경쟁 아님.
        """
        wait_for: Dict[int, int] = {}
        for rid, robot in self.robot_manager.robots.items():
            if not self._is_blocked(rid):
                continue
            if not robot.command_queue or robot.command_queue[0] != "forward":
                continue
            occupier = self._robot_at(self._get_next_node_by_heading(rid))
            if occupier is not None and occupier != rid:
                wait_for[rid] = occupier
        # 통행권 모델: 줄 서서 기다리는 staging 로봇도 wait-for에 포함.
        # (빈 command_queue라 위 루프가 놓침 — 이게 옛 staging 교착이 안 풀리던 구조적 결함.)
        # staging 로봇은 자기 목표 회랑을 점유한 로봇을 기다린다.
        for rid in self.robot_manager.robots:
            if rid in wait_for or not self._is_staging_robot(rid):
                continue
            target_ws = self._staging_target_ws(rid)
            if target_ws is None:
                continue
            owner = self.reservation.corridor_owner(target_ws)
            if owner is not None and owner != rid:
                wait_for[rid] = owner
        return find_wait_cycle(wait_for)

    def _staging_target_ws(self, rid: int) -> Optional[int]:
        """staging 큐에서 대기 중인 rid의 목표 작업대 노드 (없으면 None)."""
        for ws_node, corridor in self.staging_manager.corridors.items():
            for staged in corridor.queue:
                if staged.rid == rid:
                    return ws_node
        return None

    def _resolve_deadlock(self, cycle: List[int]) -> bool:
        """교착 사이클 해소: 멤버 1명(양보자)을 contested 노드 피해 우회 재계획 (수정 54).

        사이클은 링크 하나만 끊으면 사슬로 풀린다 → 전원 동시 재계획 불필요(재교착 위험).
        양보자가 가려던 노드(contested)를 excluded_transit에 넣어 A*가 bypass(row1/row6)로
        우회 → 양보자가 자기 현재 노드를 비우면 뒤 로봇이 전진, 나머지는 _try_dispatch_all
        재시도로 자연 unwind. 양보자는 결정론(낮은 rid부터), A* 실패 시 다음 멤버 시도.
        """
        for yielder in sorted(cycle):
            y = self.robot_manager.get_robot(yielder)
            if not y or not y.planned_path:
                continue
            contested = self._get_next_node_by_heading(yielder)
            goal = y.planned_path[-1]
            print(f"[RequestHandler] 교착 사이클 {cycle}: AGV-{yielder} 우회 재계획 "
                  f"(exclude={contested})")
            res = self._plan_and_publish_move(
                yielder, y.current_node, goal,
                extra_excluded={contested} if contested is not None else None,
            )
            if res is not None:
                return True
        print(f"[RequestHandler] 교착 사이클 {cycle}: 우회 경로 없음 — 해소 실패")
        return False

    def _flush_pending_replan(self, rid: int) -> bool:
        """B-selfguard: 보류된 재계획을 상태가 fresh한 순간(마커/cmd_ack 직후)에 실행.

        in_flight이 방금 비워진 직후 호출. 보류분이 있으면 *이제* 계획한다 —
        이 시점 robot.current_node는 AGV가 방금 보고한 실제 위치라 stale일 수 없다.
        옛 plan은 폐기되므로, 호출자는 True를 받으면 옛 경로 기반 후속 처리를 건너뛴다.

        Returns:
            True: 보류분 있어 재계획 실행 / False: 보류분 없음
        """
        if rid not in self._pending_replan:
            return False
        goal, is_forwarding = self._pending_replan.pop(rid)
        robot = self.robot_manager.get_robot(rid)
        if robot is None:
            return False
        self._plan_and_publish_move(
            rid, robot.current_node, goal, is_forwarding=is_forwarding
        )
        return True

    # ─── 큐 기반 노드 예약 조회 (REFACTOR E 3.1) ───

    def _is_node_reserved_by(self, node: int, by_rid: int) -> bool:
        """node가 by_rid의 in_flight 또는 pending head forward 목적지인가?

        _reserved_nodes set/clear 대체. CommandQueue의 peek_expected_node가
        같은 정보를 더 정확히 제공 (마커 도착 시 ack로 자동 clear).
        """
        queue = self.command_queues.get(by_rid)
        return queue is not None and queue.peek_expected_node() == node

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

    # ─── 헬퍼 (선반/대기 노드) ───

    def _diagnose_suboptimal_path(self, rid, start, goal, node_path,
                                  excluded_transit, soft_avoid, heading) -> None:
        """[진단] 최단보다 긴 경로를 골랐으면 A*의 입력을 전부 찍는다. 아니면 침묵.

        재현이 불가능한 것(예약 스냅샷)을 추측으로 메우지 않기 위한 계측.
        동작에는 영향 없음 — 읽기만 한다.
        """
        try:
            from collections import deque
            # 제약 없는 순수 최단 (BFS)
            q, seen = deque([(start, [start])]), {start}
            shortest = None
            while q:
                n, p = q.popleft()
                if n == goal:
                    shortest = p
                    break
                for nb, _ in self.path_planner.graph.get(n, []):
                    if nb not in seen:
                        seen.add(nb)
                        q.append((nb, p + [nb]))
            if not shortest or len(node_path) <= len(shortest):
                return                      # 최단이거나 더 짧다 → 조용히 넘어감

            # 최단경로의 어느 노드가 왜 막혔나
            blocked = []
            for n in shortest[1:-1]:
                why = []
                if excluded_transit and n in excluded_transit:
                    why.append("excluded_transit")
                if soft_avoid and n in soft_avoid:
                    why.append("soft_avoid(비용만)")
                if not self.reservation.is_free(n, 1, exclude_rid=rid):
                    # 누가 잡고 있나 — 무기한(회랑) / 시간기반(경로) 구분
                    indef = self.reservation._indefinite_by_node.get(n)
                    cells = [(t, r) for (nn, t), r in self.reservation._cells.items() if nn == n]
                    if indef is not None:
                        why.append(f"예약점유:무기한(AGV-{indef})")
                    if cells:
                        why.append("예약점유:시간기반" + str(sorted(cells)))
                    if indef is None and not cells:
                        why.append("예약점유:출처불명★")
                for orid, o in self.robot_manager.robots.items():
                    if orid != rid and o.current_node == n:
                        why.append(f"AGV-{orid} 위치({o.status.value})")
                blocked.append(f"{n}[{'/'.join(why) if why else '막힌 이유 없음 ★'}]")

            print(f"[진단] Robot {rid}: {start}→{goal} 최단보다 +{len(node_path)-len(shortest)}칸")
            print(f"   선택: {node_path}")
            print(f"   최단: {shortest}")
            print(f"   최단경로 중간노드 상태: {' '.join(blocked)}")
            print(f"   excluded_transit: {sorted(excluded_transit) if excluded_transit else '없음'}")
            print(f"   soft_avoid      : {sorted(soft_avoid) if soft_avoid else '없음'}")
            print(f"   heading: {heading}°  / 다른 AGV: "
                  + ", ".join(f"AGV-{r}@{o.current_node}({o.status.value})"
                              for r, o in self.robot_manager.robots.items() if r != rid))
        except Exception as e:
            print(f"[진단] 실패(무시): {e}")

    def _get_occupied_shelf_nodes(self) -> Set[int]:
        """현재 선반이 놓여있는 노드 집합 (IN_PLACE 상태인 선반)"""
        occupied = set()
        for shelf in self.shelf_manager.shelves.values():
            if shelf.status == ShelfStatus.IN_PLACE:
                occupied.add(shelf.current_node)
        return occupied

    def _turn_hazard_kind(self, rid: int, node: int) -> Optional[str]:
        """수정 82(안전망): node에서 든 채 회전 시 위험물 종류.
        None=안전 / "정적선반"(안 비킴→내려놓고돌기 필수) / "든로봇"(움직임→기다리면 됨) /
        "정적선반+든로봇". 직교 이웃에 서있는 선반(IN_PLACE/AT_WORKSTATION) 또는 다른 든
        로봇이 있으면 위험. (수정 84 측정용: 발동 원인이 정적선반 vs 든로봇 어느 쪽인지.)"""
        shelf_hz = robot_hz = False
        for nb in self.path_planner.neighbors(node):
            if not shelf_hz:
                for shelf in self.shelf_manager.shelves.values():
                    if shelf.status != ShelfStatus.CARRIED and shelf.current_node == nb:
                        shelf_hz = True
                        break
            if not robot_hz:
                for orid, other in self.robot_manager.robots.items():
                    if orid == rid or other.carrying_shelf is None:
                        continue
                    # 수정 85: 정지 위치(current_node)뿐 아니라 '진입 예정 노드'도 본다.
                    # 상대가 nb로 이동 중이면 서버상 current_node는 아직 nb가 아니지만
                    # (in-transit 사각지대), 그 선반이 nb로 들어오는 중이라 위험은 실재.
                    oq = self.command_queues.get(orid)
                    entering = oq.peek_expected_node() if oq is not None else None
                    if other.current_node == nb or entering == nb:
                        robot_hz = True
                        break
        if shelf_hz and robot_hz:
            return "정적선반+든로봇"
        if shelf_hz:
            return "정적선반"
        if robot_hz:
            return "든로봇"
        return None

    def _intransit_carrier_adjacent(self, rid: int, node: int) -> bool:
        """node의 직교 이웃으로 '이동 중(in_flight forward)'인 다른 든 로봇이 진입 중인가.

        수정 84: 든로봇 위험에 '대기'가 안전하려면 상대가 실제로 움직여야 한다 —
        그 로봇의 마커 도착이 _try_dispatch_all 재시도를 유발해 대기가 곧 풀린다.
        정지한 든 로봇 옆(current_node==nb)은 안 비키고 재시도 트리거도 없어 → 대기 금지.
        """
        for nb in self.path_planner.neighbors(node):
            for orid, other in self.robot_manager.robots.items():
                if orid == rid or other.carrying_shelf is None:
                    continue
                oq = self.command_queues.get(orid)
                if oq is not None and oq.peek_expected_node() == nb:
                    return True
        return False

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

        # 수정 82(안전망 '내려놓고 돌기'): 든 로봇이 회전하려는데 직교 옆칸에 '서있는 선반'
        # 또는 '다른 든 로봇'이 있으면, 든 채 돌면 선반이 부풀어(s/√2) 충돌한다. → 내려놓고
        # 돌고 다시 든다: [turn] → [lift_down_hold, turn, lift_up_hold]. **발행 직전 '지금'
        # 위치로 검사**하므로 시공간 예측 드리프트가 없다. 이미 감싸졌으면(다음이 lift_up_hold)
        # 재삽입 안 함(무한루프 방지). hold lift는 물리 동작만 — 상태 오염 없음(수정 82 in_place).
        # holding_turn: 물리적으로 선반을 '든 채' 제자리 회전하려는가? (수정 85)
        #   ★물리적으로 이미 든 경우만. 픽업/반납/포워딩의 pre-lift turn은 큐에 lift_up이
        #   남아있다(수정 79) — 그땐 carrying_shelf가 세팅됐어도 선반은 아직 바닥에 있다.
        #   '내려놓고 돌기'(수정 82) 중의 turn은 다음이 lift_up_hold → 이미 내려놨으므로 제외.
        holding_turn = (
            next_cmd in ("turn_left", "turn_right", "turn_180")
            and robot.carrying_shelf is not None
            and "lift_up" not in robot.command_queue
            and not (len(robot.command_queue) >= 2
                     and robot.command_queue[1] == "lift_up_hold")
        )
        _hazard = None
        if SAFETY_NET_ENABLED and holding_turn:
            _hazard = self._turn_hazard_kind(rid, robot.current_node)
        if _hazard:
            # 수정 84: 위험물이 '든로봇'뿐(움직임)이면 곧 비키므로 내려놓지 말고 잠깐 대기.
            # '정적선반'이 끼면(안 비킴) 즉시 내려놓고 돌기. 무한대기(대칭교착) 방지 위해
            # 든로봇 대기 N회 초과 시 폴백=내려놓고 돌기(양쪽이 내려놓으면 위험 소멸).
            defer = getattr(self, "_turn_defer_count", None)
            if defer is None:
                defer = self._turn_defer_count = {}
            # 대기는 (정적선반 없음) AND (이동 중 든 로봇이 이웃 진입 중)일 때만 안전.
            # 정지 든로봇 옆은 안 비키고 재시도 트리거도 없어 대기 금지(→ 내려놓고 돌기).
            only_moving = ("정적선반" not in _hazard
                           and self._intransit_carrier_adjacent(rid, robot.current_node))
            if only_moving and defer.get(rid, 0) < TURN_DEFER_MAX:
                defer[rid] = defer.get(rid, 0) + 1
                print(f"[RequestHandler] Robot {rid}: 든 채 회전 위험 @{robot.current_node} "
                      f"[위험물: {_hazard}] → 대기 (defer {defer[rid]}/{TURN_DEFER_MAX}, "
                      f"움직이는 로봇이라 곧 비킴)")
                return False   # 턴 보류 — _try_dispatch_all이 상대 이탈 시 재시도
            # 정적선반 포함 / 정지 든로봇 옆 / 든로봇 대기 N회 초과 → 내려놓고 돌기
            defer.pop(rid, None)
            if "정적선반" in _hazard:
                _why = "정적선반=못 기다림"
            elif only_moving:
                _why = "든로봇 대기초과→폴백"
            else:
                _why = "정지 든로봇=대기 무의미"
            print(f"[RequestHandler] Robot {rid}: 든 채 회전 위험 @{robot.current_node} "
                  f"[위험물: {_hazard}] → 내려놓고 돌기 ({_why})")
            robot.command_queue[:1] = ["lift_down_hold", next_cmd, "lift_up_hold"]
            next_cmd = robot.command_queue[0]   # = "lift_down_hold"
        else:
            # 위험 해소(또는 non-holding) → 이 로봇의 defer 카운터 리셋
            defer = getattr(self, "_turn_defer_count", None)
            if defer is not None:
                defer.pop(rid, None)
            if holding_turn:
                # 수정 85(순서2): 지금 이웃은 비었지만 회전하는 '동안' 다른 든 로봇이 이웃칸
                # 으로 진입할 수 있다(in-transit). 회전 = 이웃 4칸을 회전 시간만큼 점유한다는
                # 물리 사실을 footprint 예약으로 표현 → 진입 forward가 is_turn_locked로 대기.
                self.reservation.reserve_turn_footprint(
                    rid, self.path_planner.neighbors(robot.current_node))

        # forward 명령일 때만 충돌 체크
        if next_cmd == "forward":
            next_node = self._get_next_node_by_heading(rid)
            if next_node is None:
                print(f"[RequestHandler] Robot {rid}: blocked → no forward target "
                      f"(heading={robot.heading}°, node={robot.current_node})")
                return False
            # 수정 85(순서2): 다른 로봇이 든 채 회전 중인 이웃칸(footprint)엔 진입 금지 → 대기.
            lock_owner = self.reservation.turn_lock_owner(next_node)
            if lock_owner is not None and lock_owner != rid:
                print(f"[RequestHandler] Robot {rid}: blocked → node {next_node} "
                      f"(AGV-{lock_owner} 회전중)")
                return False
            for other_rid, other in self.robot_manager.robots.items():
                if other_rid == rid:
                    continue
                # 수정 75: 한 번도 안 켠 AGV는 바닥에 없다 → 막으면 안 된다.
                # A* 계획부(_plan_and_publish_move)는 이미 ever_seen으로 게이팅하는데,
                # 이 진입 직전 점유 체크가 빠져 있어 계획은 통과시킨 칸을 런타임이 막았다
                # (실측: AGV-2 미접속인데 홈 노드 32가 AGV-1 경로를 영구 차단 → 데드락).
                if not other.ever_seen:
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
        elif next_cmd in ("lift_down_hold", "lift_up_hold"):
            # 수정 82: 제자리 유지 lift → 물리 명령은 lift_down/lift_up, in_place=True로 표시.
            # ack가 워크플로우 전이로 안 가고 다음 명령만 발행되게(선반 상태 오염 방지).
            entry = CommandEntry(cmd=next_cmd.replace("_hold", ""), in_place=True)
        else:
            entry = CommandEntry(cmd=next_cmd)
        if queue is not None:
            queue.enqueue(entry)
            queue.dispatch()

        # 명령 전송 (큐 dispatch 후 publish — I1)
        robot.command_queue.pop(0)
        # 수정 82: hold lift는 AGV엔 물리 명령(lift_down/lift_up)으로 보낸다(하드웨어 무변경).
        publish_cmd_name = next_cmd.replace("_hold", "") if next_cmd.endswith("_hold") else next_cmd
        # 약점 3: lift_up/lift_down 시 대상 선반을 함께 전달 → 시뮬이 좌표 추측 대신
        # 이 선반을 직접 처리. lift_up 발행 시점엔 carrying_shelf가 이미 목표 선반으로
        # 세팅돼 있고(픽업 직전), lift_down 시엔 운반 중 선반이 곧 놓을 선반.
        shelf_id = robot.carrying_shelf if publish_cmd_name in ("lift_up", "lift_down") else None
        # 수정 70: forward의 목적지를 명시적으로 실어 보낸다.
        # 서버는 이미 안다(next_node). 안 알려주면 AGV/트윈이 각자 heading으로 추측하고,
        # heading이 갈리는 순간 같은 명령을 서로 다른 목적지로 해석한다 → 교착.
        target = next_node if next_cmd == "forward" else None
        self.mqtt_publisher.publish_cmd(rid, publish_cmd_name, shelf_id, target_node=target)
        return True

    # _predict_heading_after_inflight 제거 (B-selfguard): 계획은 in_flight None일 때만
    # 일어나므로 robot.heading이 항상 ground-truth. in-flight heading 예측 불필요.

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
        self, rid: int, start: int, goal: int, is_forwarding: bool = False,
        extra_excluded: Optional[Set[int]] = None,
        lift_cmd: Optional[str] = None,
    ) -> Optional[Dict]:
        """로봇 이동 경로 계획 → 명령 큐 생성 → 첫 명령 전송

        lift_cmd (수정 79): "lift_up"/"lift_down"이면 큐의 **첫 forward 앞에** 끼운다.
            → [turn, lift_cmd, forward…]. 선반을 안 든 채로 회전(turn)한 뒤 리프트하기 위함
            (든 채 회전하면 옆 선반과 충돌). None이면 순수 이동.


        Args:
            is_forwarding: True면 staging 시 corridor 밖 staging_node 대신
                           진입 경로 위 gateway_node에서 대기 (선반 들고 멀리 우회 방지)
            extra_excluded: A* 통과 금지 노드 추가분 (수정 54: head-on 해소 시
                            승자 위치를 막아 우회 강제). start/goal은 자동 제외.
        """
        # ── B-selfguard: 이동 중(in-flight)이면 지금 계획하지 않고 보류 ──
        # 핵심 불변식: 계획은 오직 상태가 fresh한 순간(마커/cmd_ack 직후 = in_flight None)에만
        # 한다. in-flight면 robot.current_node가 stale(아직 다음 노드 미도착)이라 거기서 계획하면
        # 명령이 한 칸 밀린다(수정 51). → 여기서 막고 _pending_replan에 등록 → 다음 마커/ack 때
        # _flush_pending_replan이 fresh 상태로 실행. 이 함수를 *누가 직접 부르든* 자동으로 안전.
        queue = self.command_queues.get(rid)
        if queue is not None and queue.in_flight is not None:
            self._pending_replan[rid] = (goal, is_forwarding)
            return None

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
        # (B-selfguard 통과 = in_flight None = robot이 실제로 이 노드에 정지해 있음)
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
            # 수정 80: 든 채 회전하면 선반이 부풀어 옆 선반과 충돌 → 위험 회전이
            # 몰리는 노드(20·28, 맵에서 유도)를 통과 금지해 그 위에서 돌 일 자체를 없앤다.
            excluded_transit |= self.path_planner.carry_forbidden_nodes
        # 통행권 모델 (2026-06-23): 미래 timeline 예측 commit 제거 — 드리프트 원천 제거.
        # 충돌은 _send_next_command의 진입 직전 점유 체크(현재노드 + in-flight 예약)가 막고,
        # A*는 "지금" 상태만 본다 — 정지 로봇/선반은 hard 회피, 움직이는 로봇 경로는 soft 회피.
        # corridor 점유(indefinite)는 self.reservation.is_free가 그대로 막아줌(commit과 무관).
        soft_avoid: Set[int] = set()
        for other_rid, other in self.robot_manager.robots.items():
            if other_rid == rid:
                continue
            # 수정 75: 한 번도 안 켠 AGV는 바닥에 없다 → 길을 막으면 안 된다.
            # (통신이 끊긴 AGV는 ever_seen=True 로 남아 계속 장애물이다 — 몸은 그 자리에 있다.)
            if not other.ever_seen:
                continue
            stationary = (other.status == RobotStatus.IDLE
                          or not other.planned_path
                          or self._is_staging_robot(other_rid))
            if stationary:
                # 주차/대기/staging = 안 비킴 → hard 장애물 (경로가 통과 못 함)
                excluded_transit.add(other.current_node)
            else:
                # 움직이는 로봇 현재 위치 + 남은 경로 → soft 회피 (되도록 피하되 공유 가능)
                soft_avoid.add(other.current_node)
                soft_avoid.update(other.planned_path)
            # 선반 반납 중이면 그 선반의 원위치 노드도 hard 제외
            if other.status == RobotStatus.RETURNING_SHELF and other.carrying_shelf is not None:
                shelf_obj = self.shelf_manager.shelves.get(other.carrying_shelf)
                if shelf_obj:
                    excluded_transit.add(shelf_obj.home_node)
        # (구 Step 1 "staging_node 무조건 transit 제외" 삭제 — 통행권이 안전 보장.
        #  빈 회랑은 통행 허용 → 불필요한 회전 제거. 차 있으면 corridor indefinite가 is_free로 막음.)
        # 수정 54: head-on 해소용 추가 제외 노드 (승자 위치 막아 우회 강제)
        if extra_excluded:
            excluded_transit |= {n for n in extra_excluded
                                 if n != start and n != actual_goal}
        if not excluded_transit:
            excluded_transit = None

        # B-selfguard 불변식 tripwire: 여기 도달 = in_flight None 이어야 함.
        # 누가 위 가드를 제거/우회해 in-flight 중 계획에 진입하면 즉시 실패시켜
        # stale 위치 기반 계획(수정 51 클래스)을 조용히 넘기지 않는다.
        assert queue is None or queue.in_flight is None, (
            f"B-selfguard 위반: rid={rid} in-flight 중 계획 시도 — "
            f"_plan_and_publish_move를 직접 호출하지 말 것"
        )

        # 수정 81: 재계획이 '미실행 lift_up'을 날려 빈 이동하는 버그 방지.
        # 든 채(포워딩/반납) 출발 큐 [turn, lift_up, forward…]에서 turn만 발행되고 lift_up이
        # 아직 큐에 남은 순간 교착 재계획 등이 걸리면, 새 큐엔 lift_up이 없어 로봇이 선반을
        # 그 자리에 두고 안 든 채 움직인다(선반 분실 + Isaac 'shelf None'). 미실행 lift_up이
        # 남아 있으면 재주입해 물리적 픽업을 보존한다. (lift_down은 도착 후 append라 해당 없음.)
        if lift_cmd is None and robot and "lift_up" in robot.command_queue:
            lift_cmd = "lift_up"
        # A* 경로 계획 (reservation 기반 시공간 충돌 회피)
        # in_flight None이 보장되므로 robot.heading이 곧 ground-truth (예측 불필요)
        planning_heading = robot.heading if robot else None
        timed_path = self.path_planner.astar_with_time(
            start=start,
            goal=actual_goal,
            reservation=self.reservation,
            rid=rid,
            max_time=self.config.max_time,
            excluded_transit=excluded_transit,
            start_heading=planning_heading,
            soft_avoid=soft_avoid or None,
        )

        if timed_path is None:
            print(f"[RequestHandler] No path found for Robot {rid}: {start} -> {actual_goal}")
            return None

        node_path = PathPlanner.compress_to_node_path(timed_path)

        # [진단] 최단경로보다 긴 경로를 골랐다면 **그 자리에서 이유를 통째로 남긴다.**
        #
        # 실측(2026-07-13): 경로 계획 35건 중 7건(20%)이 최단보다 **정확히 +2칸** 길었고,
        # 전부 작업대(9/33)가 출발지 또는 목적지였다. 작업대 이웃은 3개뿐이라(9→[1,10,17])
        # 그중 하나(게이트웨이)가 막히면 우회가 정확히 +2가 된다.
        # 그런데 excluded_transit/선반/정지로봇만으로는 재현이 안 됐다 → 남은 건 reservation인데
        # 그건 로그로 재구성이 불가능하다(시간 기반 스냅샷).
        #
        # 그래서 추측 대신 **현장에서 잡는다.** 20% 확률이라 금방 걸린다.
        # 평소엔 조용하다 — 최단을 골랐으면 아무것도 안 찍는다.
        self._diagnose_suboptimal_path(rid, start, actual_goal, node_path,
                                       excluded_transit, soft_avoid, planning_heading)

        # 명령 큐 생성 및 저장
        if robot:
            robot.planned_path = node_path
            # REFACTOR F Phase 3: 자기 plan reservation 등록 (dwell=1 legacy 호환)
            #
            # [수정 74 검증 기록] 이 commit을 지우면 안 된다.
            # 죽은 예약(유령) 때문에 지워봤더니 53건 만에 AGV 2대가 41↔42에서 정면으로
            # 맞물려 완전히 멈췄다. commit이 박는 건 cell만이 아니라 **edge(스왑 예약)**
            # 이고, 그 edge가 "쟤가 B→A로 갈 예정이면 나는 A→B 못 감" = 정면충돌을
            # 계획 단계에서 차단하는 유일한 장치였다. 41은 이웃이 33/42뿐이라 한번
            # 맞물리면 교착 해소(수정 54)도 빠져나갈 길을 못 찾는다.
            # → 유령 예약은 commit을 지우는 게 아니라 **청소부를 되살려서** 고친다 (아래).
            self.reservation.commit(rid, node_path, dwell=1)
            # commands 생성도 동일한 예측 heading 사용 (A*와 정합)
            robot.command_queue = self._path_to_commands(node_path, planning_heading)
            # 수정 79: 첫 forward 앞에 lift 명령 끼우기 (안 든 채 회전 → 리프트 → 전진)
            if lift_cmd:
                fwd_i = next((k for k, c in enumerate(robot.command_queue)
                              if c == "forward"), len(robot.command_queue))
                robot.command_queue.insert(fwd_i, lift_cmd)
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
