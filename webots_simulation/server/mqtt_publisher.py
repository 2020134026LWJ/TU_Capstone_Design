"""
MQTT 발행 모듈
- /agv/plan 토픽으로 경로 발행
- 연결 관리
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

# paho-mqtt 버전 호환성 처리
try:
    from paho.mqtt.enums import CallbackAPIVersion
    PAHO_V2 = True
except ImportError:
    PAHO_V2 = False

from .config import Config


class MQTTPublisher:
    """MQTT 발행기"""

    def __init__(self, config: Config):
        self.config = config
        self.client: Optional[mqtt.Client] = None
        self.connected = False

    def connect(self) -> bool:
        """MQTT 브로커 연결"""
        try:
            if PAHO_V2:
                self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
            else:
                self.client = mqtt.Client()
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.connect(self.config.mqtt_host, self.config.mqtt_port, 60)
            self.client.loop_start()
            time.sleep(0.5)  # 연결 대기
            print(f"[MQTTPublisher] Connected to {self.config.mqtt_host}:{self.config.mqtt_port}")
            return True
        except Exception as e:
            print(f"[MQTTPublisher] Connection failed: {e}")
            return False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # paho-mqtt 1.x: rc는 int, 2.x: rc는 ReasonCode 객체
        rc_val = rc if isinstance(rc, int) else getattr(rc, 'value', rc)
        if rc_val == 0:
            self.connected = True
            print(f"[MQTTPublisher] Connected, rc={rc}")
        else:
            print(f"[MQTTPublisher] Connection failed, rc={rc}")

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        print(f"[MQTTPublisher] Disconnected")

    def disconnect(self) -> None:
        """MQTT 연결 해제"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            print("[MQTTPublisher] Disconnected")

    def publish_plan(
        self,
        robots: List[Dict[str, Any]],
        speed: float = 0.3
    ) -> bool:
        """
        경로 계획 발행

        Args:
            robots: 로봇 정보 리스트
                [{"rid": 0, "start": 1, "goal": 45, "node_path": [1,2,...], "timed_path": [{"node":1,"t":0},...]}, ...]
            speed: 이동 속도

        Returns:
            발행 성공 여부
        """
        if not self.client or not self.connected:
            print("[MQTTPublisher] Not connected")
            return False

        payload = {
            "job_id": int(time.time()),
            "planner": "prioritized_astar_with_time_on_graph",
            "robots": robots,
            "speed": speed
        }

        try:
            self.client.publish(
                self.config.mqtt_topic_plan,
                json.dumps(payload),
                qos=0
            )
            print(f"[MQTTPublisher] Published plan to {self.config.mqtt_topic_plan}")
            return True
        except Exception as e:
            print(f"[MQTTPublisher] Publish failed: {e}")
            return False

    def publish_single_robot_plan(
        self,
        rid: int,
        start: int,
        goal: int,
        timed_path: List[Tuple[int, int]],
        speed: float = 0.3
    ) -> bool:
        """단일 로봇 경로 발행"""
        from .path_planner import PathPlanner

        node_path = PathPlanner.compress_to_node_path(timed_path)

        robots = [{
            "rid": rid,
            "start": start,
            "goal": goal,
            "node_path": node_path,
            "timed_path": [{"node": n, "t": t} for (n, t) in timed_path]
        }]

        return self.publish_plan(robots, speed)

    def publish_shelf_command(self, rid: int, command: str, shelf_id: int) -> bool:
        """
        선반 명령 발행 (pickup / putdown)

        Args:
            rid: 로봇 ID
            command: "pickup" 또는 "putdown"
            shelf_id: 선반 ID
        """
        if not self.client or not self.connected:
            print("[MQTTPublisher] Not connected")
            return False

        payload = {
            "rid": rid,
            "command": command,
            "shelf_id": shelf_id,
            "timestamp": time.time(),
        }

        try:
            self.client.publish(
                self.config.mqtt_topic_shelf_cmd,
                json.dumps(payload),
                qos=0,
            )
            print(f"[MQTTPublisher] Published shelf_cmd: {command} shelf {shelf_id} by robot {rid}")
            return True
        except Exception as e:
            print(f"[MQTTPublisher] Shelf command publish failed: {e}")
            return False

    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return self.connected
