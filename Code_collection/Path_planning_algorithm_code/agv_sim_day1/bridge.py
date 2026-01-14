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


class Bridge:
    def __init__(self, my_rid: int = 0) -> None:
        # 이 브릿지가 담당하는 로봇 번호(0, 1, ...)
        self.my_rid = my_rid

        self.current_plan: Optional[Dict[str, Any]] = None
        self.path: List[int] = []
        self.idx: int = 0
        self.speed: float = 0.3

        self.current_node: Optional[int] = None
        self.progress: float = 0.0

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[BRIDGE] connected, reason={reason_code}")
        client.subscribe(TOPIC_PLAN)
        client.subscribe(TOPIC_STATE)
        print(f"[BRIDGE] subscribed: {TOPIC_PLAN}, {TOPIC_STATE}")

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
        self.current_plan = plan
        self.speed = float(plan.get("speed", 0.3))

        # (1) 새 server.py 형식: robots 배열이 있는 경우
        if "robots" in plan:
            robots = plan.get("robots", [])
            mine = None
            for r in robots:
                if int(r.get("rid", -1)) == self.my_rid:
                    mine = r
                    break

            if mine is None:
                print(f"[BRIDGE] no plan for rid={self.my_rid}")
                self.path = []
                return

            self.path = mine.get("node_path", [])

        else:
            # (2) 예전 형식(단일 로봇): path가 바로 오는 경우
            self.path = plan.get("path", [])

        self.idx = 1 if len(self.path) > 1 else 0
        self.current_node = self.path[0] if self.path else None
        self.progress = 0.0

        print(f"[BRIDGE] got PLAN: rid={self.my_rid}, path={self.path}, speed={self.speed}")

    def handle_state(self, state: Dict[str, Any]) -> None:
        # (선택) state에 rid가 들어오면, 내 로봇 것만 처리
        if "rid" in state and int(state["rid"]) != self.my_rid:
            return

        self.current_node = state.get("current_node", self.current_node)
        self.progress = float(state.get("progress", self.progress))

        if self.path and self.idx < len(self.path):
            target = self.path[self.idx]
            if self.current_node == target:
                self.idx += 1
                print(f"[BRIDGE] reached node {target} -> next idx={self.idx}")

    def tick(self) -> None:
        if not self.path:
            return

        done = self.idx >= len(self.path)
        target_node = self.path[-1] if done else self.path[self.idx]

        highcmd = {
            "rid": self.my_rid,
            "mode": "FOLLOW_PATH",
            "target_node": int(target_node),
            "speed": float(self.speed),
            "done": bool(done),
            "path": self.path
        }

        lowcmd = {
            "rid": self.my_rid,
            "v": float(self.speed if not done else 0.0),
            "w": 0.0,
            "target_node": int(target_node)
        }

        self.client.publish(TOPIC_HIGHCMD, json.dumps(highcmd), qos=0)
        self.client.publish(TOPIC_LOWCMD, json.dumps(lowcmd), qos=0)

        print(f"[BRIDGE] pub highcmd -> {highcmd}")
        print(f"[BRIDGE] pub lowcmd  -> {lowcmd}")

    def run(self) -> None:
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()
        try:
            while True:
                self.tick()
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[BRIDGE] exit")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    Bridge(my_rid=0).run()
