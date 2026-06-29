"""공통 MQTT 통신 모듈 (실물/시뮬 동일)"""

import json
import time

# ─── MQTT 토픽 ───
TOPIC_PLAN = "/agv/plan"
TOPIC_STATE = "/agv/state"
TOPIC_ARRIVED = "/agv/arrived"
TOPIC_SHELF_CMD = "/agv/shelf_cmd"
TOPIC_SHELF_ACK = "/agv/shelf_ack"
TOPIC_MARKER = "/agv/marker"
TOPIC_CONTROL = "/agv/control"

# ─── 기본 설정 ───
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1883


class MQTTHandler:
    """AGV MQTT 통신 핸들러"""

    def __init__(self, rid: int, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.rid = rid
        self._host = host
        self._port = port
        self._client = None
        self._connected = False
        self._callbacks = {}  # topic -> callback

    def register_callback(self, topic: str, callback):
        """토픽별 콜백 등록 (connect 호출 전에 등록)"""
        self._callbacks[topic] = callback

    def connect(self):
        """MQTT 브로커 연결 (콜백 등록 후 호출)"""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print(f"[AGV {self.rid}] paho-mqtt not available")
            return

        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        try:
            self._client.connect(self._host, self._port, 60)
            self._client.loop_start()
            print(f"[AGV {self.rid}] MQTT connecting...")
        except Exception as e:
            print(f"[AGV {self.rid}] MQTT connection failed: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = True
        for topic in self._callbacks:
            client.subscribe(topic)
        topics = ", ".join(self._callbacks.keys())
        print(f"[AGV {self.rid}] MQTT connected, subscribed: {topics}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return

        callback = self._callbacks.get(msg.topic)
        if callback:
            callback(data)

    @property
    def connected(self) -> bool:
        return self._connected

    # ─── 발행 메서드 ───

    def publish_arrived(self, node: int):
        if not self._connected:
            return
        msg = {"type": "robot_arrived", "rid": self.rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))
        print(f"[AGV {self.rid}] → arrived at node {node}")

    def publish_position(self, node: int):
        if not self._connected:
            return
        msg = {"type": "robot_position", "rid": self.rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))

    def publish_shelf_ack(self, shelf_id: int, command: str):
        if not self._connected:
            return
        msg = {"rid": self.rid, "command": command, "shelf_id": shelf_id, "status": "done"}
        self._client.publish(TOPIC_SHELF_ACK, json.dumps(msg))
        print(f"[AGV {self.rid}] → shelf_ack: {command} shelf {shelf_id}")

    def publish_marker(self, marker_id: int):
        if not self._connected:
            return
        msg = {"rid": self.rid, "marker_id": marker_id, "ts": int(time.time())}
        self._client.publish(TOPIC_MARKER, json.dumps(msg), qos=0)

    def publish_state(self, state_dict: dict):
        if not self._connected:
            return
        try:
            self._client.publish(TOPIC_STATE, json.dumps(state_dict), qos=0)
        except Exception:
            pass

    def disconnect(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
