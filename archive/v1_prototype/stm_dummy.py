import json
import time
from typing import Optional

import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_LOWCMD = "/agv/lowcmd"
TOPIC_STATE = "/agv/state"


class STMDummy:
    def __init__(self) -> None:
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        # 데모 상태
        self.current_node: Optional[int] = None
        self.target_node: Optional[int] = None
        self.progress: float = 0.0  # 0.0~1.0 (목표 노드로 가는 진행률)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"[STM_DUMMY] connected, reason={reason_code}")
        client.subscribe(TOPIC_LOWCMD)
        print(f"[STM_DUMMY] subscribed: {TOPIC_LOWCMD}")

    def on_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[STM_DUMMY] JSON decode error: {e}")
            return

        v = float(cmd.get("v", 0.0))
        new_target = cmd.get("target_node", None)

        if new_target is None:
            return

        new_target = int(new_target)

        # 첫 상태 초기화
        if self.current_node is None:
            # 처음에는 "현재노드"를 목표와 다르게 시작시키기 어려우니,
            # 데모상 current_node를 new_target으로 시작하고 progress를 0으로 둠
            self.current_node = new_target
            self.target_node = new_target
            self.progress = 0.0

        # 목표가 바뀌면 진행률 초기화
        if self.target_node != new_target:
            self.target_node = new_target
            self.progress = 0.0

        # 데모 규칙:
        # v > 0이면 progress 상승
        # progress가 1.0이 되면 current_node가 target_node로 "도착"
        if v > 0.0:
            self.progress += 0.34  # 3번 정도 받으면 도착하게
            if self.progress >= 1.0:
                self.progress = 1.0
                self.current_node = self.target_node

        state = {
            "current_node": int(self.current_node),
            "progress": round(self.progress, 2),
            "target_node": int(self.target_node),
            "ts": int(time.time())
        }

        self.client.publish(TOPIC_STATE, json.dumps(state), qos=0)
        print(f"[STM_DUMMY] pub state -> {state}")

    def run(self) -> None:
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[STM_DUMMY] exit")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    STMDummy().run()
