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
import os
import time
import uuid
from typing import Callable, Optional

import paho.mqtt.client as mqtt

# ─── 설정 ───

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_CMD     = "/agv/cmd"
TOPIC_MARKER  = "/agv/marker"
TOPIC_CMD_ACK = "/agv/cmd_ack"
TOPIC_CMD_START = "/agv/cmd_start"   # 트윈 전용 — 실물의 동작 시작(STM EVT_ACK) 수신
TOPIC_PRESENCE = "/agv/presence"   # 수정 75 — AGV 접속/이탈 (일반 시뮬만 발행, 트윈은 침묵)

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

    def __init__(self, rid: int, cmd_handler: Callable,
                 marker_handler: Optional[Callable] = None,
                 ack_handler: Optional[Callable] = None,
                 start_handler: Optional[Callable] = None):
        self.rid = rid
        self._cmd_handler = cmd_handler
        # 트윈 모드 전용: 실물의 /agv/cmd_start(동작 시작=STM EVT_ACK)를 구독 → 그 시각에
        # 애니메이션을 시작한다(실물이 실제로 움직이기 시작한 순간과 출발을 맞춤).
        self._start_handler = start_handler
        # 트윈 모드 전용: 실물 AGV가 발행한 /agv/marker를 구독해 위치를 따라간다.
        # None(기본, 일반 시뮬)이면 구독조차 하지 않음 → 자기가 발행한 마커를 되받지 않는다.
        self._marker_handler = marker_handler
        # 트윈 모드 전용: 실물의 /agv/cmd_ack(회전·리프트 완료)를 구독 → 그 시간에 맞춰
        # 애니메이션을 끝낸다 (수정 60). 일반 모드는 자기가 발행하므로 구독하면 되받는다.
        self._ack_handler = ack_handler

        # 수정 63: client_id는 반드시 전역 유일해야 한다.
        #
        # 예전엔 f"bridge_{rid}_{int(time.time())}" 였는데, 초 단위 타임스탬프라
        # **트윈과 실물/벤치가 같은 초에 뜨면 id가 똑같아진다**. MQTT는 같은 client_id를
        # 허용하지 않아 브로커가 먼저 붙은 쪽을 끊고, 끊긴 쪽이 재연결하며 상대를 다시
        # 끊는 **무한 재연결 루프**에 빠진다 (실측: 10초에 4회씩, 명령 유실).
        # 재현이 타이밍에 달려 있어 잡기 어려운 종류의 버그다.
        #
        # 트윈은 실물과 **역할이 다르므로 접두사도 다르게** 한다("twin_" vs "bridge_").
        # 같은 초에 떠도 절대 겹치지 않고, 브로커 쪽에서 누가 누군지 구분도 된다.
        self._client = mqtt.Client(
            client_id=f"twin_{rid}_{os.getpid()}_{uuid.uuid4().hex[:6]}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # 수정 75 — presence는 **AGV 역할일 때만** 발행한다.
        #
        # 트윈(TWIN=1)은 관찰자다. 트윈이 "AGV-2 여기 있다"고 말하면 실물 AGV-2가
        # 꺼져 있어도 서버가 있다고 믿고 태스크를 준다 — 유령을 막으려고 만든 장치가
        # 유령을 만드는 꼴이 된다. 트윈은 침묵한다(마커·cmd_ack를 침묵하는 것과 같은 이유).
        if not self._is_twin:
            self._client.will_set(       # connect() 전에 등록해야 효력 발생
                TOPIC_PRESENCE,
                json.dumps({"rid": rid, "online": False}),
                qos=1, retain=True,
            )

    @property
    def _is_twin(self) -> bool:
        """트윈 모드 = 실물의 보고를 구독하는 관찰자. 일반 시뮬은 자기가 AGV다."""
        return self._marker_handler is not None

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    def connect(self):
        self._client.connect(MQTT_HOST, MQTT_PORT, 60)
        self._client.loop_start()

    def disconnect(self):
        if not self._is_twin:
            self._publish_presence(False)   # 정상 종료는 LWT가 안 뜬다 → 직접 알림
        self._client.loop_stop()
        self._client.disconnect()

    def _publish_presence(self, online: bool):
        """수정 75 — 접속/이탈 알림 (일반 시뮬 = Isaac이 곧 AGV일 때만)."""
        self._client.publish(
            TOPIC_PRESENCE,
            json.dumps({"rid": self.rid, "online": online}),
            qos=1, retain=True,
        )

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(TOPIC_CMD)
        if self._marker_handler is not None:
            client.subscribe(TOPIC_MARKER)   # 트윈 모드 — 실물의 위치 보고를 따라감
        if self._ack_handler is not None:
            client.subscribe(TOPIC_CMD_ACK)  # 트윈 모드 — 실물의 회전/리프트 완료를 따라감
        if self._start_handler is not None:
            client.subscribe(TOPIC_CMD_START)  # 트윈 모드 — 실물의 동작 시작을 따라감
        if not self._is_twin:
            self._publish_presence(True)
        print(f"[Bridge-{self.rid}] MQTT connected (rc={rc})"
              f"{'' if self._is_twin else ' — presence online'}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic == TOPIC_CMD:
            rid = int(data.get("rid", -1))
            cmd = data.get("cmd", "")
            if rid == self.rid and cmd:
                self._dispatch_cmd(cmd, data.get("shelf_id"), data.get("target_node"))

        elif msg.topic == TOPIC_MARKER and self._marker_handler is not None:
            rid = int(data.get("rid", -1))
            marker_id = data.get("marker_id")
            if rid == self.rid and marker_id is not None:
                self._marker_handler(self.rid, int(marker_id))

        elif msg.topic == TOPIC_CMD_ACK and self._ack_handler is not None:
            rid = int(data.get("rid", -1))
            cmd = data.get("cmd", "")
            if rid == self.rid and cmd:
                self._ack_handler(self.rid, cmd)

        elif msg.topic == TOPIC_CMD_START and self._start_handler is not None:
            rid = int(data.get("rid", -1))
            cmd = data.get("cmd", "")
            if rid == self.rid and cmd:
                self._start_handler(self.rid, cmd)

    def _dispatch_cmd(self, cmd: str, shelf_id: Optional[int] = None,
                      target_node: Optional[int] = None):
        print(f"[Bridge-{self.rid}] <- cmd: {cmd}")
        # Isaac Sim 모드: 콜백 호출 (lift 대상 shelf_id, forward 목적지 전달)
        self._cmd_handler(self.rid, cmd, shelf_id, target_node)

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
