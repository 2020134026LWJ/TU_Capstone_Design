"""
AGV Bridge (Isaac Sim 전용) — cmd 콜백 모드
TU Capstone Design - AGV 물류 피킹 시스템

  서버(MQTT) → Bridge → cmd_handler 콜백 → IsaacAGV
  ※ 실물 RPi(UART, 주원이 ASCII 프로토콜) 버전은 bridge_rpi.py

MQTT 토픽:
  수신: /agv/cmd         서버 → AGV 명령
  송신: /agv/marker      AGV 위치/방향 보고 (marker_id + heading)
        /agv/cmd_ack     명령 완료 보고 (lift는 shelf_id 포함 — 수정 56)
"""

import json
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

# ─── 설정 ───

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_CMD     = "/agv/cmd"
TOPIC_MARKER  = "/agv/marker"
TOPIC_CMD_ACK = "/agv/cmd_ack"

# cmd_ack의 shelf_id "미지정" 센티넬 (lift_up/down만 대상 선반 명시)
_ACK_SHELF_UNSET = object()


class Bridge:
    """
    AGV 콜백 브릿지 (Isaac Sim 전용, 단일 로봇)

    Parameters
    ----------
    rid : int
        로봇 ID (1 or 2)
    cmd_handler : callable
        cmd_handler(rid: int, cmd: str, shelf_id: int | None) → None
        Isaac Sim: agv._on_cmd_from_bridge
    """

    def __init__(self, rid: int, cmd_handler: Callable):
        self.rid = rid
        self._cmd_handler = cmd_handler

        self._client = mqtt.Client(client_id=f"bridge_{rid}_{int(time.time())}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    def connect(self):
        self._client.connect(MQTT_HOST, MQTT_PORT, 60)
        self._client.loop_start()

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(TOPIC_CMD)
        print(f"[Bridge-{self.rid}] MQTT connected (rc={rc})")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic == TOPIC_CMD:
            rid = int(data.get("rid", -1))
            cmd = data.get("cmd", "")
            if rid == self.rid and cmd:
                self._dispatch_cmd(cmd, data.get("shelf_id"))

    def _dispatch_cmd(self, cmd: str, shelf_id: Optional[int] = None):
        print(f"[Bridge-{self.rid}] <- cmd: {cmd}")
        # Isaac Sim 모드: 콜백 호출 (lift 대상 shelf_id 전달)
        self._cmd_handler(self.rid, cmd, shelf_id)

    # ─── 발행 ─────────────────────────────────────────────────────────────────

    def publish_marker(self, marker_id: int, heading_deg: int):
        """ArUco 마커 감지 결과 서버에 보고"""
        msg = {
            "rid": self.rid,
            "marker_id": marker_id,
            "heading": heading_deg,
            "ts": int(time.time()),
        }
        self._client.publish(TOPIC_MARKER, json.dumps(msg))
        print(f"[Bridge-{self.rid}] -> /agv/marker  id={marker_id}  heading={heading_deg}°")

    def publish_cmd_ack(self, cmd: str, shelf_id=_ACK_SHELF_UNSET):
        """명령 완료 서버에 보고.

        lift_up/lift_down은 실제로 들/놓은 선반(shelf_id)을 함께 보고 (수정 56 약점4).
        lift_up인데 shelf_id=None이면 '빈 리프트' → 서버가 감지·복구.
        """
        msg = {"type": "cmd_ack", "rid": self.rid, "cmd": cmd, "status": "done"}
        reported = cmd in ("lift_up", "lift_down") and shelf_id is not _ACK_SHELF_UNSET
        if reported:
            msg["shelf_id"] = shelf_id
        self._client.publish(TOPIC_CMD_ACK, json.dumps(msg))
        suffix = f"  shelf={shelf_id}" if reported else ""
        print(f"[Bridge-{self.rid}] -> /agv/cmd_ack  cmd={cmd}{suffix}")
