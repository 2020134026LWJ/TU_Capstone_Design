"""
MQTT 발행 모듈
- /agv/plan 토픽으로 경로 발행
- 연결 관리
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

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
            self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
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
        if rc == 0 or rc.value == 0:
            self.connected = True
            print(f"[MQTTPublisher] Connected, rc={rc}")
        else:
            print(f"[MQTTPublisher] Connection failed, rc={rc}")

    def _on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None):
        self.connected = False
        print(f"[MQTTPublisher] Disconnected, rc={rc}")

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

    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return self.connected
