import json
import heapq
import time
from typing import Dict, List, Tuple, Optional

import paho.mqtt.client as mqtt


def load_map(path: str) -> Tuple[Dict[int, Tuple[float, float]], Dict[int, List[Tuple[int, float]]]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    nodes = {n["id"]: (n.get("x", 0.0), n.get("y", 0.0)) for n in data["nodes"]}
    graph: Dict[int, List[Tuple[int, float]]] = {nid: [] for nid in nodes.keys()}

    for e in data["edges"]:
        a, b, c = int(e["from"]), int(e["to"]), float(e.get("cost", 1.0))
        graph.setdefault(a, []).append((b, c))

    return nodes, graph


def heuristic(nodes: Dict[int, Tuple[float, float]], a: int, b: int) -> float:
    ax, ay = nodes.get(a, (0.0, 0.0))
    bx, by = nodes.get(b, (0.0, 0.0))
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def astar(nodes, graph, start: int, goal: int) -> Optional[List[int]]:
    open_heap: List[Tuple[float, int]] = []
    heapq.heappush(open_heap, (0.0, start))

    came_from: Dict[int, int] = {}
    g_score: Dict[int, float] = {start: 0.0}

    while open_heap:
        _, current = heapq.heappop(open_heap)

        if current == goal:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path

        for nxt, cost in graph.get(current, []):
            tentative = g_score[current] + cost
            if tentative < g_score.get(nxt, float("inf")):
                came_from[nxt] = current
                g_score[nxt] = tentative
                f = tentative + heuristic(nodes, nxt, goal)
                heapq.heappush(open_heap, (f, nxt))

    return None


def main():
    MAP_FILE = "map.json"
    START = 1
    GOAL = 4

    MQTT_HOST = "localhost"
    MQTT_PORT = 1883
    TOPIC_PLAN = "/agv/plan"

    nodes, graph = load_map(MAP_FILE)
    path = astar(nodes, graph, START, GOAL)

    if not path:
        print("[SERVER] No path found.")
        return

    payload = {
        "job_id": int(time.time()),
        "start": START,
        "goal": GOAL,
        "path": path,
        "speed": 0.3
    }

    print(f"[SERVER] computed path: {path}")
    print(f"[SERVER] publishing to {TOPIC_PLAN}: {payload}")

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
