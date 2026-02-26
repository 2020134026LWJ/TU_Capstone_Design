import json
import heapq
import time
from typing import Dict, List, Tuple, Optional, Any

import paho.mqtt.client as mqtt


# ============================================================
# 0. map.json 로더 (유지)
# ------------------------------------------------------------
# map.json을 읽어서:
# - nodes: {node_id: (x, y)}
# - graph: {node_id: [(neighbor_id, cost), ...]}
# 형태로 변환한다.
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
# 1. 휴리스틱 함수
# ------------------------------------------------------------
# 그래프 노드에 (x,y)가 있으므로 유클리드 거리로 휴리스틱을 만든다.
# A*에서 f = g + h
# - g: 지금까지 실제 비용(시간/거리 비용)
# - h: 목표까지의 추정 비용(낙관적이어야 최단성 보장)
# ============================================================
def heuristic(nodes: Dict[int, Tuple[float, float]], a: int, b: int) -> float:
    ax, ay = nodes.get(a, (0.0, 0.0))
    bx, by = nodes.get(b, (0.0, 0.0))
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


# ============================================================
# 2. 시간 포함 A* (그래프 버전)
# ------------------------------------------------------------
# 상태(state) = (node_id, t)
#
# reserved_nodes: set[(node_id, t)]
#   - 특정 시간 t에 특정 노드를 다른 로봇이 점유하면 충돌이므로 금지
#
# reserved_edges: set[(u, v, t)]
#   - 시간 t -> t+1 동안 u->v 이동이 예약돼 있으면 기록
#   - 다른 로봇이 같은 시간에 v->u로 스왑(교차)하는 것을 금지하기 위해 사용
#
# 반환값:
#   - 성공: [(node_id, t), ...]
#   - 실패: None
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
    start_state = (start, 0)  # (node_id, t=0)

    # open_heap: (f_score, g_score, node_id, t)
    open_heap: List[Tuple[float, float, int, int]] = []
    heapq.heappush(open_heap, (heuristic(nodes, start, goal), 0.0, start, 0))

    came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
    g_score: Dict[Tuple[int, int], float] = {start_state: 0.0}

    while open_heap:
        f, g, cur_node, t = heapq.heappop(open_heap)

        # 목표 노드에 도달하면 경로 복원
        if cur_node == goal:
            path: List[Tuple[int, int]] = [(cur_node, t)]
            cur = (cur_node, t)
            while cur in came_from:
                cur = came_from[cur]
                path.append(cur)
            path.reverse()
            return path

        # 시간 제한 초과면 더 확장하지 않음
        if t >= max_time:
            continue

        # 다음 시각
        nt = t + 1

        # 다음 후보들:
        # 1) 대기: cur_node 유지
        # 2) 이동: graph[cur_node]의 인접 노드로 이동
        neighbors: List[Tuple[int, float]] = [(cur_node, 1.0)]  # (next_node, step_cost) - 대기는 cost=1로 둠(시간 1칸)
        for nxt, cost in graph.get(cur_node, []):
            neighbors.append((nxt, float(cost)))

        for nxt_node, step_cost in neighbors:
            next_state = (nxt_node, nt)

            # (A) 정적 충돌(노드 점유) 방지:
            # 다른 로봇이 같은 시간(nt)에 nxt_node를 점유하면 금지
            if (nxt_node, nt) in reserved_nodes:
                continue

            # (B) 스왑(Edge conflict) 방지:
            # 내가 cur->nxt로 가는데, 다른 로봇이 같은 시간에 nxt->cur로 가면 충돌
            # 이동하는 경우에만 체크(대기는 제외)
            if nxt_node != cur_node:
                if (nxt_node, cur_node, t) in reserved_edges:
                    continue

            # g 갱신
            tentative_g = g + step_cost
            if tentative_g < g_score.get(next_state, float("inf")):
                g_score[next_state] = tentative_g
                came_from[next_state] = (cur_node, t)
                f_next = tentative_g + heuristic(nodes, nxt_node, goal)
                heapq.heappush(open_heap, (f_next, tentative_g, nxt_node, nt))

    return None


# ============================================================
# 3. Prioritized Planning (우선순위 기반 다중 로봇 경로계획)
# ------------------------------------------------------------
# starts/goals의 인덱스 순서가 곧 우선순위라고 가정한다.
# - rid=0 로봇이 가장 우선순위가 높다.
#
# 흐름:
# 1) reserved_nodes/edges를 비워둔 상태에서 시작
# 2) 우선순위 높은 로봇부터 차례대로 시간 포함 A*로 경로를 찾음
# 3) 찾은 경로를 reserved에 등록해서, 다음 로봇이 피하도록 함
# 4) goal에 도착 후 stay_time_at_goal 동안 머물도록 goal 점유도 예약
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
    reserved_nodes = set()  # (node_id, t)
    reserved_edges = set()  # (u, v, t) : t->t+1 동안 u->v 이동

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

        # 이 로봇의 경로를 예약 테이블에 등록
        # - 노드 점유 예약
        # - 이동 간선 예약(스왑 충돌 방지용)
        for i in range(len(path)):
            node_i, t_i = path[i]
            reserved_nodes.add((node_i, t_i))

            if i + 1 < len(path):
                node_j, t_j = path[i + 1]
                # t_j는 t_i+1이어야 정상(시간 포함 A* 특성)
                # 이동한 경우에만 edge 예약
                if node_j != node_i:
                    reserved_edges.add((node_i, node_j, t_i))

        # 목표 도착 후 일정 시간 머무르게 예약(다른 로봇이 들이받지 않도록)
        goal_node, goal_t = path[-1]
        for dt in range(1, stay_time_at_goal + 1):
            reserved_nodes.add((goal_node, goal_t + dt))

        paths[rid] = path

    # Optional 제거
    return [p for p in paths if p is not None]


# ============================================================
# 4. MQTT publish payload 구성 유틸
# ------------------------------------------------------------
# 실제 로봇/브릿지가 이해하기 쉽게, 다음 두 가지 형태를 같이 만든다.
# 1) timed_path: [(node_id, t), ...]  -> 디버깅/시뮬레이션용
# 2) node_path : [node_id, ...]       -> 로봇이 따라갈 "노드 순서"용
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
# 5. main
# ------------------------------------------------------------
# - map.json 로드
# - 다중 로봇 우선순위 경로계획
# - MQTT로 /agv/plan publish
#
# 현재는 데모용으로 starts/goals를 하드코딩했다.
# 실제 시스템에서는 UI 주문, DB 재고, 작업 단계(선반/작업대/복귀)에 따라
# start/goal이 결정되고, 그때마다 이 경로계획을 호출하면 된다.
# ============================================================
def main():
    # -----------------------------
    # (1) 맵 로드
    # -----------------------------
    MAP_FILE = "map.json"
    nodes, graph = load_map(MAP_FILE)

    # -----------------------------
    # (2) 다중 로봇 시작/목표 설정 (데모)
    # -----------------------------
    # map.json의 node id 기반으로 작성
    # 예시: 로봇 2대
    starts = [1, 6]
    goals  = [4, 3]

    # -----------------------------
    # (3) 우선순위 기반 다중 로봇 경로 계획
    # -----------------------------
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

    # -----------------------------
    # (4) MQTT payload 구성
    # -----------------------------
    robots_payload: List[Dict[str, Any]] = []
    for rid, timed_path in enumerate(paths):
        robots_payload.append({
            "rid": rid,
            "start": starts[rid],
            "goal": goals[rid],
            "node_path": compress_to_node_path(timed_path),
            "timed_path": [{"node": n, "t": t} for (n, t) in timed_path]
        })

    payload = {
        "job_id": int(time.time()),
        "planner": "prioritized_astar_with_time_on_graph",
        "robots": robots_payload,
        "speed": 0.3
    }

    # -----------------------------
    # (5) MQTT publish
    # -----------------------------
    MQTT_HOST = "localhost"
    MQTT_PORT = 1883
    TOPIC_PLAN = "/agv/plan"

    print("[SERVER] computed multi-robot paths:")
    for r in robots_payload:
        print(f"  rid={r['rid']} node_path={r['node_path']}")

    print(f"[SERVER] publishing to {TOPIC_PLAN}: job_id={payload['job_id']} robots={len(robots_payload)}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    client.publish(TOPIC_PLAN, json.dumps(payload), qos=0)
    time.sleep(0.5)

    client.loop_stop()
    client.disconnect()
    print("[SERVER] done.")


if __name__ == "__main__":
    main()
