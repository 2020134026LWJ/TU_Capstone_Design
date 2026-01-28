"""
AGV Bridge for Webots Simulation
TU Capstone Design - AGV 물류 피킹 시스템

다중 로봇 지원 브릿지
- /agv/plan 수신 → 경로 파싱
- /agv/state 수신 → 상태 업데이트
- /agv/lowcmd 발행 → 로봇 제어 명령
"""

import json
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_PLAN = "/agv/plan"
TOPIC_HIGHCMD = "/agv/highcmd"
TOPIC_LOWCMD = "/agv/lowcmd"
TOPIC_STATE = "/agv/state"


class RobotState:
    """개별 로봇 상태"""
    def __init__(self, rid: int):
        self.rid = rid
        self.path: List[int] = []
        self.idx: int = 0
        self.speed: float = 0.3
        self.current_node: Optional[int] = None
        self.progress: float = 0.0
        self.done: bool = False


class MultiBridge:
    """다중 로봇 브릿지"""
    def __init__(self, num_robots: int = 2):
        self.num_robots = num_robots
        self.robots: Dict[int, RobotState] = {}

        for rid in range(num_robots):
            self.robots[rid] = RobotState(rid)

        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, rc):
        print(f"[BRIDGE] Connected, rc={rc}")
        client.subscribe(TOPIC_PLAN)
        client.subscribe(TOPIC_STATE)
        print(f"[BRIDGE] Subscribed: {TOPIC_PLAN}, {TOPIC_STATE}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[BRIDGE] JSON decode error on {msg.topic}: {e}")
            return

        if msg.topic == TOPIC_PLAN:
            self.handle_plan(payload)
        elif msg.topic == TOPIC_STATE:
            self.handle_state(payload)

    def handle_plan(self, plan: Dict[str, Any]) -> None:
        """경로 계획 수신"""
        speed = float(plan.get("speed", 0.3))

        if "robots" not in plan:
            print("[BRIDGE] No 'robots' in plan")
            return

        for robot_plan in plan.get("robots", []):
            rid = int(robot_plan.get("rid", -1))
            if rid not in self.robots:
                continue

            robot = self.robots[rid]
            robot.path = robot_plan.get("node_path", [])
            robot.speed = speed
            robot.idx = 1 if len(robot.path) > 1 else 0
            robot.current_node = robot.path[0] if robot.path else None
            robot.progress = 0.0
            robot.done = False

            print(f"[BRIDGE] AGV {rid}: path={robot.path}, speed={speed}")

    def handle_state(self, state: Dict[str, Any]) -> None:
        """로봇 상태 수신"""
        rid = state.get("rid")
        if rid is None or rid not in self.robots:
            return

        robot = self.robots[rid]
        robot.current_node = state.get("current_node", robot.current_node)
        robot.progress = float(state.get("progress", robot.progress))

        # 목표 노드 도달 체크
        if robot.path and robot.idx < len(robot.path):
            target = robot.path[robot.idx]
            if robot.current_node == target:
                robot.idx += 1
                print(f"[BRIDGE] AGV {rid}: reached node {target}, next idx={robot.idx}")

                if robot.idx >= len(robot.path):
                    robot.done = True
                    print(f"[BRIDGE] AGV {rid}: COMPLETED path")

    def tick(self) -> None:
        """주기적 명령 발행"""
        for rid, robot in self.robots.items():
            if not robot.path:
                continue

            target_node = robot.path[-1] if robot.done else robot.path[min(robot.idx, len(robot.path) - 1)]
            v = 0.0 if robot.done else robot.speed

            lowcmd = {
                "rid": rid,
                "v": float(v),
                "w": 0.0,
                "target_node": int(target_node)
            }

            self.client.publish(TOPIC_LOWCMD, json.dumps(lowcmd), qos=0)

            if not robot.done:
                print(f"[BRIDGE] AGV {rid}: -> node {target_node}, v={v:.2f}")

    def run(self) -> None:
        """메인 루프"""
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()

        print(f"[BRIDGE] Running with {self.num_robots} robots...")

        try:
            while True:
                self.tick()
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[BRIDGE] Exit")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    import sys
    num_robots = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    MultiBridge(num_robots=num_robots).run()
