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

        print(f"[PathPlanner] Loaded {len(self.nodes)} nodes "
              f"(M={len(self.nodes) - len(self.shelf_nodes) - len(self.workstation_nodes)}, "
              f"S={len(self.shelf_nodes)}, W={len(self.workstation_nodes)}) "
              f"from {self.map_file}")
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

    def get_node_type(self, node_id: int) -> str:
        """노드 타입 반환"""
        return self.node_types.get(node_id, "M")

    def astar_with_time(
        self,
        start: int,
        goal: int,
        reserved_nodes: Set[Tuple[int, int]],
        reserved_edges: Set[Tuple[int, int, int]],
        max_time: int = 50,
        excluded_transit: Optional[Set[int]] = None
    ) -> Optional[List[Tuple[int, int]]]:
        """
        시간 포함 A* 알고리즘

        Args:
            start: 시작 노드
            goal: 목표 노드
            reserved_nodes: 예약된 노드 집합 {(node_id, time), ...}
            reserved_edges: 예약된 엣지 집합 {(from_node, to_node, time), ...}
            max_time: 최대 시간
            excluded_transit: 통과 불가 노드 집합 (start/goal 제외)

        Returns:
            시간 포함 경로 [(node, time), ...] 또는 None
        """
        start_state = (start, 0)

        open_heap: List[Tuple[float, float, int, int]] = []
        heapq.heappush(open_heap, (self._heuristic(start, goal), 0.0, start, 0))

        came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
        g_score: Dict[Tuple[int, int], float] = {start_state: 0.0}

        while open_heap:
            f, g, cur_node, t = heapq.heappop(open_heap)

            if cur_node == goal:
                path: List[Tuple[int, int]] = [(cur_node, t)]
                cur = (cur_node, t)
                while cur in came_from:
                    cur = came_from[cur]
                    path.append(cur)
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

                next_state = (nxt_node, nt)

                # 노드 충돌 검사
                if (nxt_node, nt) in reserved_nodes:
                    continue

                # 엣지 충돌 검사 (스왑 충돌)
                if nxt_node != cur_node:
                    if (nxt_node, cur_node, t) in reserved_edges:
                        continue

                tentative_g = g + step_cost
                if tentative_g < g_score.get(next_state, float("inf")):
                    g_score[next_state] = tentative_g
                    came_from[next_state] = (cur_node, t)
                    f_next = tentative_g + self._heuristic(nxt_node, goal)
                    heapq.heappush(open_heap, (f_next, tentative_g, nxt_node, nt))

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
