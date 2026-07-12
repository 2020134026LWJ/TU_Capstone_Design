"""
MQTT 발행 모듈
- /agv/cmd 토픽으로 개별 명령 발행
- 연결 관리
"""

import json
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

# paho-mqtt 버전 호환성 처리
try:
    from paho.mqtt.enums import CallbackAPIVersion
    PAHO_V2 = True
except ImportError:
    PAHO_V2 = False

from ..config import Config


class MQTTClient:
    """MQTT 발행/구독"""

    def __init__(self, config: Config):
        self.config = config
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self._subscriptions: Dict[str, Any] = {}  # topic → callback

    def connect(self) -> bool:
        """MQTT 브로커 연결"""
        try:
            if PAHO_V2:
                self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
            else:
                self.client = mqtt.Client()
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.connect(self.config.mqtt_host, self.config.mqtt_port, 60)
            self.client.loop_start()
            time.sleep(0.5)  # 연결 대기
            print(f"[MQTTClient] Connected to {self.config.mqtt_host}:{self.config.mqtt_port}")
            return True
        except Exception as e:
            print(f"[MQTTClient] Connection failed: {e}")
            return False

    def subscribe(self, topic: str, callback) -> None:
        """MQTT 토픽 구독 + 콜백 등록"""
        self._subscriptions[topic] = callback
        if self.client and self.connected:
            self.client.subscribe(topic)
            print(f"[MQTTClient] Subscribed: {topic}")

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # paho-mqtt 1.x: rc는 int, 2.x: rc는 ReasonCode 객체
        rc_val = rc if isinstance(rc, int) else getattr(rc, 'value', rc)
        if rc_val == 0:
            self.connected = True
            # 재연결 시 구독 복원
            for topic in self._subscriptions:
                client.subscribe(topic)
                print(f"[MQTTClient] Re-subscribed: {topic}")
            print(f"[MQTTClient] Connected, rc={rc}")
        else:
            print(f"[MQTTClient] Connection failed, rc={rc}")

    def _on_message(self, client, userdata, msg):
        """구독 메시지 수신 → 콜백 호출"""
        callback = self._subscriptions.get(msg.topic)
        if not callback:
            return
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            callback(data)
        except Exception as e:
            print(f"[MQTTClient] Message handling error on {msg.topic}: {e}")

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        print(f"[MQTTClient] Disconnected")

    def disconnect(self) -> None:
        """MQTT 연결 해제"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            print("[MQTTClient] Disconnected")

    def publish_cmd(self, rid: int, cmd: str, shelf_id: Optional[int] = None,
                    target_node: Optional[int] = None) -> bool:
        """
        AGV 개별 명령 발행

        Args:
            rid: 로봇 ID
            cmd: "forward" | "backward" | "turn_left" | "turn_right" | "turn_180"
                 | "lift_up" | "lift_down"
            shelf_id: lift_up/lift_down 시 대상 선반 (약점 3 — 시뮬이 추측 대신
                      이 선반을 직접 들도록. 실물은 무시). None이면 미지정.
            target_node: forward의 도착 예정 노드 (수정 70).

                **왜 필요한가**: 예전엔 `{"cmd": "forward"}`만 보냈다. 그러면 AGV/트윈이
                **자기 heading을 보고 "내 앞이 어디지?"를 스스로 계산**해야 한다.
                그런데 heading은 서버·AGV·트윈이 각자 장부로 추적하는 값이라, 한 번
                어긋나면 **같은 forward 명령을 서로 다른 목적지로 해석한다.**

                실측(2026-07-12 통합테스트): 유령 마커 때문에 트윈이 heading을 자가복구 →
                그 순간부터 서버는 `forward 목표=25`, 트윈은 `forward → node 9`로 갈렸다.
                서버는 도착 보고를 "목표와 다른 마커"라며 거부하고 AGV는 영영 진행 불가 —
                **교착.** 실물에서도 heading이 한 번 틀어지면 똑같이 터진다.

                **서버는 이미 목적지를 안다**(`in_flight.target_node`). 그냥 알려주면
                아무도 추측할 필요가 없고, **"각자 계산해서 갈라지는" 버그 종류가 사라진다.**
                (실물 AGV는 물리적으로 앞으로 갈 뿐이라 이 값을 무시해도 된다)
        """
        if not self.client or not self.connected:
            print("[MQTTClient] Not connected")
            return False

        payload = {"rid": rid, "cmd": cmd, "timestamp": time.time()}
        if shelf_id is not None:
            payload["shelf_id"] = shelf_id
        if target_node is not None:
            payload["target_node"] = target_node

        try:
            self.client.publish(self.config.mqtt_topic_cmd, json.dumps(payload), qos=0)
            print(f"[MQTTClient] AGV {rid} <- {cmd}")
            return True
        except Exception as e:
            print(f"[MQTTClient] Publish failed: {e}")
            return False

    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return self.connected
