"""
AGV Server for Webots Simulation
TU Capstone Design - AGV 물류 피킹 시스템

9x5 그리드 맵 기반 다중 로봇 경로 계획 서버
- map.json 로드
- Prioritized A* 경로 계획
- MQTT로 경로 발행
"""

import json
import heapq
import time
import os
from typing import Dict, List, Tuple, Optional, Any

import paho.mqtt.client as mqtt


# ============================================================
# 설정
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(SCRIPT_DIR, "map.json")

MQTT_HOST = "localhost"
MQTT_PORT = 1883
TOPIC_PLAN = "/agv/plan"


# ============================================================
# map.json 로더
# ============================================================
def load_map(path: str) -> Tuple[Dict[int, Tuple[float, float]], Dict[int, List[Tuple[int, float]]]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = {int(n["id"]): (float(n.get("x", 0.0)), float(n.get("y", 0.0))) for n in data["nodes"]}
    graph: Dict[int, List[Tuple[int, float]]] = {nid: [] for nid in nodes.keys()}

    for e in data["edges"]:
        a, b, c = int(e["from"]), int(e["to"]), float(e.get("cost", 1.0))
        graph.setdefault(a, []).append((b, c))

    return nodes, graph


# ============================================================
# 휴리스틱 함수
# ============================================================
def heuristic(nodes: Dict[int, Tuple[float, float]], a: int, b: int) -> float:
    ax, ay = nodes.get(a, (0.0, 0.0))
    bx, by = nodes.get(b, (0.0, 0.0))
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


# ============================================================
# 시간 포함 A* (그래프 버전)
# ============================================================
def astar_with_time_on_graph(
    nodes: Dict[int, Tuple[float, float]],
    graph: Dict[int, List[Tuple[int, float]]],
    start: int,
    goal: int,
    reserved_nodes: set,
    reserved_edges: set,
    max_time: int = 50
) -> Optional[List[Tuple[int, int]]]:
    start_state = (start, 0)

    open_heap: List[Tuple[float, float, int, int]] = []
    heapq.heappush(open_heap, (heuristic(nodes, start, goal), 0.0, start, 0))

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

        neighbors: List[Tuple[int, float]] = [(cur_node, 1.0)]
        for nxt, cost in graph.get(cur_node, []):
            neighbors.append((nxt, float(cost)))

        for nxt_node, step_cost in neighbors:
            next_state = (nxt_node, nt)

            if (nxt_node, nt) in reserved_nodes:
                continue

            if nxt_node != cur_node:
                if (nxt_node, cur_node, t) in reserved_edges:
                    continue

            tentative_g = g + step_cost
            if tentative_g < g_score.get(next_state, float("inf")):
                g_score[next_state] = tentative_g
                came_from[next_state] = (cur_node, t)
                f_next = tentative_g + heuristic(nodes, nxt_node, goal)
                heapq.heappush(open_heap, (f_next, tentative_g, nxt_node, nt))

    return None


# ============================================================
# Prioritized Planning
# ============================================================
def prioritized_planning_on_graph(
    nodes: Dict[int, Tuple[float, float]],
    graph: Dict[int, List[Tuple[int, float]]],
    starts: List[int],
    goals: List[int],
    max_time: int = 50,
    stay_time_at_goal: int = 3
) -> Optional[List[List[Tuple[int, int]]]]:
    num_robots = len(starts)
    reserved_nodes = set()
    reserved_edges = set()

    paths: List[Optional[List[Tuple[int, int]]]] = [None] * num_robots

    for rid in range(num_robots):
        start = starts[rid]
        goal = goals[rid]

        path = astar_with_time_on_graph(
            nodes, graph,
            start=start,
            goal=goal,
            reserved_nodes=reserved_nodes,
            reserved_edges=reserved_edges,
            max_time=max_time
        )

        if path is None:
            print(f"[SERVER] [WARN] robot {rid}: no path found")
            return None

        for i in range(len(path)):
            node_i, t_i = path[i]
            reserved_nodes.add((node_i, t_i))

            if i + 1 < len(path):
                node_j, t_j = path[i + 1]
                if node_j != node_i:
                    reserved_edges.add((node_i, node_j, t_i))

        goal_node, goal_t = path[-1]
        for dt in range(1, stay_time_at_goal + 1):
            reserved_nodes.add((goal_node, goal_t + dt))

        paths[rid] = path

    return [p for p in paths if p is not None]


# ============================================================
# 노드 경로 압축
# ============================================================
def compress_to_node_path(timed_path: List[Tuple[int, int]]) -> List[int]:
    node_path: List[int] = []
    last = None
    for node, _t in timed_path:
        if last is None or node != last:
            node_path.append(node)
            last = node
    return node_path


# ============================================================
# 그리드 시각화 (디버깅용)
# ============================================================
def print_grid(nodes, starts, goals):
    """9x5 그리드를 콘솔에 출력"""
    print("\n[SERVER] 9x5 Grid Map:")
    print("=" * 40)
    for row in range(5):
        line = ""
        for col in range(9):
            node_id = row * 9 + col + 1
            if node_id in starts:
                rid = starts.index(node_id)
                line += f" S{rid} "
            elif node_id in goals:
                rid = goals.index(node_id)
                line += f" G{rid} "
            else:
                line += f" {node_id:2d} "
        print(line)
    print("=" * 40)


# ============================================================
# main
# ============================================================
def main():
    # (1) 맵 로드
    print(f"[SERVER] Loading map: {MAP_FILE}")
    nodes, graph = load_map(MAP_FILE)
    print(f"[SERVER] Loaded {len(nodes)} nodes")

    # (2) 다중 로봇 시작/목표 설정
    # 9x5 그리드 기준:
    #  1  2  3  4  5  6  7  8  9
    # 10 11 12 13 14 15 16 17 18
    # 19 20 21 22 23 24 25 26 27
    # 28 29 30 31 32 33 34 35 36
    # 37 38 39 40 41 42 43 44 45

    # AGV 0: 노드 1에서 시작 → 노드 45로 이동 (좌상단 → 우하단)
    # AGV 1: 노드 37에서 시작 → 노드 9로 이동 (좌하단 → 우상단)
    starts = [1, 37]
    goals = [45, 9]

    print_grid(nodes, starts, goals)

    # (3) 경로 계획
    print("[SERVER] Computing paths...")
    paths = prioritized_planning_on_graph(
        nodes, graph,
        starts=starts,
        goals=goals,
        max_time=50,
        stay_time_at_goal=3
    )

    if paths is None:
        print("[SERVER] No multi-robot path found.")
        return

    # (4) MQTT payload 구성
    robots_payload: List[Dict[str, Any]] = []
    for rid, timed_path in enumerate(paths):
        node_path = compress_to_node_path(timed_path)
        robots_payload.append({
            "rid": rid,
            "start": starts[rid],
            "goal": goals[rid],
            "node_path": node_path,
            "timed_path": [{"node": n, "t": t} for (n, t) in timed_path]
        })

    payload = {
        "job_id": int(time.time()),
        "planner": "prioritized_astar_with_time_on_graph",
        "robots": robots_payload,
        "speed": 0.3
    }

    # (5) MQTT publish
    print("\n[SERVER] Computed paths:")
    for r in robots_payload:
        print(f"  AGV {r['rid']}: {r['start']} → {r['goal']}")
        print(f"         path: {r['node_path']}")

    print(f"\n[SERVER] Publishing to {TOPIC_PLAN}...")

    client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    client.publish(TOPIC_PLAN, json.dumps(payload), qos=0)
    time.sleep(0.5)

    client.loop_stop()
    client.disconnect()
    print("[SERVER] Done.")


if __name__ == "__main__":
    main()
