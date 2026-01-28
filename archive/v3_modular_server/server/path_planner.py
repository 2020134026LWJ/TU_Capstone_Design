"""
경로 계획 모듈
- A* 알고리즘 (시간 포함)
- Prioritized Planning
- 맵 로드
"""

import json
import heapq
from typing import Dict, List, Tuple, Optional, Set


class PathPlanner:
    """A* 기반 경로 계획기"""

    def __init__(self, map_file: str):
        self.map_file = map_file
        self.nodes: Dict[int, Tuple[float, float]] = {}
        self.graph: Dict[int, List[Tuple[int, float]]] = {}
        self._load_map()

    def _load_map(self) -> None:
        """map.json 로드"""
        with open(self.map_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.nodes = {
            int(n["id"]): (float(n.get("x", 0.0)), float(n.get("y", 0.0)))
            for n in data["nodes"]
        }

        self.graph = {nid: [] for nid in self.nodes.keys()}

        for e in data["edges"]:
            a, b, c = int(e["from"]), int(e["to"]), float(e.get("cost", 1.0))
            self.graph.setdefault(a, []).append((b, c))

        print(f"[PathPlanner] Loaded {len(self.nodes)} nodes from {self.map_file}")

    def _heuristic(self, a: int, b: int) -> float:
        """유클리드 거리 휴리스틱"""
        ax, ay = self.nodes.get(a, (0.0, 0.0))
        bx, by = self.nodes.get(b, (0.0, 0.0))
        return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

    def is_valid_node(self, node_id: int) -> bool:
        """노드 유효성 검사"""
        return node_id in self.nodes

    def astar_with_time(
        self,
        start: int,
        goal: int,
        reserved_nodes: Set[Tuple[int, int]],
        reserved_edges: Set[Tuple[int, int, int]],
        max_time: int = 50
    ) -> Optional[List[Tuple[int, int]]]:
        """
        시간 포함 A* 알고리즘

        Args:
            start: 시작 노드
            goal: 목표 노드
            reserved_nodes: 예약된 노드 집합 {(node_id, time), ...}
            reserved_edges: 예약된 엣지 집합 {(from_node, to_node, time), ...}
            max_time: 최대 시간

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

    def prioritized_planning(
        self,
        starts: List[int],
        goals: List[int],
        max_time: int = 50,
        stay_time_at_goal: int = 3
    ) -> Optional[List[List[Tuple[int, int]]]]:
        """
        Prioritized Planning - 다중 로봇 경로 계획

        Args:
            starts: 시작 노드 리스트
            goals: 목표 노드 리스트
            max_time: 최대 시간
            stay_time_at_goal: 목표 도착 후 대기 시간

        Returns:
            각 로봇의 시간 포함 경로 리스트 또는 None
        """
        num_robots = len(starts)
        reserved_nodes: Set[Tuple[int, int]] = set()
        reserved_edges: Set[Tuple[int, int, int]] = set()

        paths: List[Optional[List[Tuple[int, int]]]] = [None] * num_robots

        for rid in range(num_robots):
            start = starts[rid]
            goal = goals[rid]

            path = self.astar_with_time(
                start=start,
                goal=goal,
                reserved_nodes=reserved_nodes,
                reserved_edges=reserved_edges,
                max_time=max_time
            )

            if path is None:
                print(f"[PathPlanner] Robot {rid}: no path found ({start} -> {goal})")
                return None

            # 경로 예약
            for i in range(len(path)):
                node_i, t_i = path[i]
                reserved_nodes.add((node_i, t_i))

                if i + 1 < len(path):
                    node_j, t_j = path[i + 1]
                    if node_j != node_i:
                        reserved_edges.add((node_i, node_j, t_i))

            # 목표 노드에서 대기 시간 예약
            goal_node, goal_t = path[-1]
            for dt in range(1, stay_time_at_goal + 1):
                reserved_nodes.add((goal_node, goal_t + dt))

            paths[rid] = path
            print(f"[PathPlanner] Robot {rid}: path found ({start} -> {goal}), length={len(path)}")

        return [p for p in paths if p is not None]

    def plan_single_robot(
        self,
        start: int,
        goal: int,
        max_time: int = 50
    ) -> Optional[List[Tuple[int, int]]]:
        """단일 로봇 경로 계획"""
        return self.astar_with_time(
            start=start,
            goal=goal,
            reserved_nodes=set(),
            reserved_edges=set(),
            max_time=max_time
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
