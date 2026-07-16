"""
경로 계획 모듈
- A* 알고리즘 (시간 포함)
- 맵 로드 (노드 타입: M=통로, S=선반, W=작업대)
- 선반 노드 통과 허용 (KIVA 스타일 - AGV가 선반 아래로 이동)
"""

import json
import heapq
from typing import Dict, List, Tuple, Optional, Set


class PathPlanner:
    """A* 기반 경로 계획기"""

    def __init__(self, map_file: str):
        self.map_file = map_file
        self.nodes: Dict[int, Tuple[float, float]] = {}
        self.node_types: Dict[int, str] = {}          # node_id -> "M"/"S"/"W"
        self.graph: Dict[int, List[Tuple[int, float]]] = {}
        self.shelf_nodes: Set[int] = set()
        self.workstation_nodes: Set[int] = set()
        # 수정 80: 선반을 든 로봇이 이 위에서 회전하면 든 선반이 부풀어(s/2→s/√2)
        # 직교 이웃의 선반과 충돌한다. "비선반인데 직교 이웃에 선반이 ≥2개"인 노드(현 맵=20,28).
        # 하드코딩 금지(규칙 11) → 맵 기하에서 유도. 든 이동 시 excluded_transit에 넣는다.
        self.carry_forbidden_nodes: Set[int] = set()
        self._load_map()

    def _load_map(self) -> None:
        """map.json 로드"""
        with open(self.map_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        for n in data["nodes"]:
            nid = int(n["id"])
            self.nodes[nid] = (float(n.get("x", 0.0)), float(n.get("y", 0.0)))
            ntype = n.get("type", "M")
            self.node_types[nid] = ntype
            if ntype == "S":
                self.shelf_nodes.add(nid)
            elif ntype == "W":
                self.workstation_nodes.add(nid)

        self.graph = {nid: [] for nid in self.nodes.keys()}

        for e in data["edges"]:
            a, b, c = int(e["from"]), int(e["to"]), float(e.get("cost", 1.0))
            self.graph.setdefault(a, []).append((b, c))

        # 수정 80: 든-채-회전 금지 노드 유도 (비선반 & 직교 이웃에 선반 ≥2개)
        self.carry_forbidden_nodes = {
            nid for nid in self.nodes
            if nid not in self.shelf_nodes
            and sum(1 for o in self.neighbors(nid) if o in self.shelf_nodes) >= 2
        }

        print(f"[PathPlanner] Loaded {len(self.nodes)} nodes "
              f"(M={len(self.nodes) - len(self.shelf_nodes) - len(self.workstation_nodes)}, "
              f"S={len(self.shelf_nodes)}, W={len(self.workstation_nodes)}) "
              f"from {self.map_file}")
        print(f"[PathPlanner] Carry-forbidden nodes (든 채 회전 금지): "
              f"{sorted(self.carry_forbidden_nodes)}")
        for ws in self.workstation_nodes:
            print(f"[PathPlanner] Workstation {ws} edges: {self.graph.get(ws, [])}")

    def _heuristic(self, a: int, b: int) -> float:
        """유클리드 거리 휴리스틱"""
        ax, ay = self.nodes.get(a, (0.0, 0.0))
        bx, by = self.nodes.get(b, (0.0, 0.0))
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    def is_valid_node(self, node_id: int) -> bool:
        """노드 유효성 검사"""
        return node_id in self.nodes

    def neighbors(self, node_id: int) -> List[int]:
        """한 칸 안에 갈 수 있는 노드들 (비용 제외)"""
        return [n for n, _ in self.graph.get(node_id, [])]

    def get_node_type(self, node_id: int) -> str:
        """노드 타입 반환"""
        return self.node_types.get(node_id, "M")

    def _node_direction(self, from_node: int, to_node: int) -> int:
        """두 노드 간 이동 방향 (0=N, 1=E, 2=S, 3=W)"""
        fx, fy = self.nodes.get(from_node, (0.0, 0.0))
        tx, ty = self.nodes.get(to_node, (0.0, 0.0))
        dx, dy = tx - fx, ty - fy
        if abs(dx) < abs(dy):
            return 0 if dy > 0 else 2
        else:
            return 1 if dx > 0 else 3

    def calc_heading_from_path(
        self, planned_path: List[int], current_node: int
    ) -> Optional[int]:
        """경로에서 현재 노드 기준 heading(도) 계산

        Args:
            planned_path: 계획된 노드 경로 ([prev_node, ..., current_node, ...])
            current_node: 기준 노드

        Returns:
            0=N / 90=E / 180=S / 270=W, 계산 불가 시 None
        """
        if not planned_path or current_node not in planned_path:
            return None
        idx = planned_path.index(current_node)
        if idx == 0:
            return None
        prev_node = planned_path[idx - 1]
        px, py = self.nodes.get(prev_node, (None, None))
        cx, cy = self.nodes.get(current_node, (None, None))
        if px is None or cx is None:
            return None
        dx, dy = cx - px, cy - py
        if abs(dx) < abs(dy):
            return 0 if dy > 0 else 180
        else:
            return 90 if dx > 0 else 270

    def astar_with_time(
        self,
        start: int,
        goal: int,
        reservation: Optional["ReservationService"] = None,  # REFACTOR F Phase 3: 신규
        rid: Optional[int] = None,                            # reservation 사용 시 exclude_rid
        reserved_nodes: Optional[Set[Tuple[int, int]]] = None,  # legacy 경로 (테스트/하위호환)
        reserved_edges: Optional[Set[Tuple[int, int, int]]] = None,
        max_time: int = 50,           # PARAM: A* 시간 horizon (step). 길수록 우회/대기 옵션 ↑, 비용 ↑
        excluded_transit: Optional[Set[int]] = None,
        turn_penalty: float = 0.3,    # PARAM: 방향 전환 추가 비용 (0=무시). ↑하면 직선 선호, 회전 ↓
        start_heading: Optional[int] = None,  # 서버 기준 degree (0=N,90=E,180=S,270=W)
        soft_avoid: Optional[Set[int]] = None,  # 통행권: 되도록 피할 노드(움직이는 로봇 경로). 비용만 +
        soft_penalty: float = 2.0,    # PARAM: soft_avoid 노드 통과 시 추가 비용 (회피 강도)
    ) -> Optional[List[Tuple[int, int]]]:
        """
        시간 포함 A* 알고리즘 (회전 페널티 포함)

        Args:
            start: 시작 노드
            goal: 목표 노드
            reservation: 시공간 예약 서비스 (제공 시 reserved_nodes/edges 무시).
            rid: reservation 사용 시 자기 자신 ID (exclude_rid).
            reserved_nodes: legacy 노드 예약 집합 (reservation 없을 때 사용).
            reserved_edges: legacy 엣지 예약 집합.
            max_time: 최대 시간
            excluded_transit: 통과 불가 노드 집합 (start/goal 제외)
            turn_penalty: 방향 전환 시 추가 비용 (0=페널티 없음, 기본 0.3)
            start_heading: 출발 방향 (degree) — None이면 방향 무관

        Returns:
            시간 포함 경로 [(node, time), ...] 또는 None
        """
        use_reservation = reservation is not None
        if not use_reservation:
            # legacy 경로: 기본 빈 set
            if reserved_nodes is None:
                reserved_nodes = set()
            if reserved_edges is None:
                reserved_edges = set()
        def heading_to_dir(h: int) -> int:
            return {0: 0, 90: 1, 180: 2, 270: 3}.get(h, -1)

        start_dir = heading_to_dir(start_heading) if start_heading is not None else -1
        # state: (node, time, dir)  dir=-1 = 방향 미정
        start_state = (start, 0, start_dir)

        open_heap: List[Tuple[float, float, int, int, int]] = []
        heapq.heappush(open_heap, (self._heuristic(start, goal), 0.0, start, 0, start_dir))

        came_from: Dict[Tuple[int, int, int], Tuple[int, int, int]] = {}
        g_score: Dict[Tuple[int, int, int], float] = {start_state: 0.0}

        while open_heap:
            f, g, cur_node, t, cur_dir = heapq.heappop(open_heap)

            if cur_node == goal:
                path: List[Tuple[int, int]] = [(cur_node, t)]
                cur_s = (cur_node, t, cur_dir)
                while cur_s in came_from:
                    cur_s = came_from[cur_s]
                    path.append((cur_s[0], cur_s[1]))
                path.reverse()
                return path

            if t >= max_time:
                continue

            nt = t + 1

            # 현재 위치에서 대기 + 인접 노드로 이동
            neighbors: List[Tuple[int, float]] = [(cur_node, 1.0)]
            for nxt, cost in self.graph.get(cur_node, []):
                neighbors.append((nxt, float(cost)))

            for nxt_node, step_cost in neighbors:
                # 선반 노드 통과 제외 (start/goal은 허용)
                if excluded_transit and nxt_node in excluded_transit:
                    if nxt_node != goal and nxt_node != start:
                        continue

                # 노드 충돌 검사
                if use_reservation:
                    if not reservation.is_free(nxt_node, nt, exclude_rid=rid):
                        continue
                else:
                    if (nxt_node, nt) in reserved_nodes:
                        continue

                # 엣지 충돌 검사 (스왑 충돌)
                if nxt_node != cur_node:
                    if use_reservation:
                        if not reservation.is_edge_free(cur_node, nxt_node, t, exclude_rid=rid):
                            continue
                    else:
                        if (nxt_node, cur_node, t) in reserved_edges:
                            continue

                # 방향 계산 및 회전 페널티
                if nxt_node != cur_node:
                    nxt_dir = self._node_direction(cur_node, nxt_node)
                    extra = turn_penalty if (cur_dir != -1 and nxt_dir != cur_dir) else 0.0
                else:
                    nxt_dir = cur_dir  # 대기: 방향 유지
                    extra = 0.0

                # 통행권: 움직이는 로봇의 경로는 되도록 피함 (목표 노드는 예외)
                if soft_avoid and nxt_node in soft_avoid and nxt_node != goal:
                    extra += soft_penalty

                tentative_g = g + step_cost + extra
                next_state = (nxt_node, nt, nxt_dir)

                if tentative_g < g_score.get(next_state, float("inf")):
                    g_score[next_state] = tentative_g
                    came_from[next_state] = (cur_node, t, cur_dir)
                    f_next = tentative_g + self._heuristic(nxt_node, goal)
                    heapq.heappush(open_heap, (f_next, tentative_g, nxt_node, nt, nxt_dir))

        return None

    def plan_single_robot(
        self,
        start: int,
        goal: int,
        max_time: int = 50
    ) -> Optional[List[Tuple[int, int]]]:
        """단일 로봇 경로 계획 (선반 노드 통과 허용)"""
        return self.astar_with_time(
            start=start,
            goal=goal,
            reserved_nodes=set(),
            reserved_edges=set(),
            max_time=max_time,
        )

    @staticmethod
    def compress_to_node_path(timed_path: List[Tuple[int, int]]) -> List[int]:
        """시간 포함 경로를 노드 경로로 압축 (대기 제거)"""
        node_path: List[int] = []
        last = None
        for node, _t in timed_path:
            if last is None or node != last:
                node_path.append(node)
                last = node
        return node_path
