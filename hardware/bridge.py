"""
AGV Bridge — cmd 기반 v2
TU Capstone Design - AGV 물류 피킹 시스템

역할:
  서버(MQTT) ↔ Bridge(RPi) ↔ STM32(UART)

동작 모드:
  RPi 실물 : cmd_handler=None  → UART로 STM32에 명령 전달
                                  UART 수신 스레드로 STM32 이벤트 처리
  Isaac Sim: cmd_handler=함수  → 콜백으로 시뮬레이터에 전달
                                  cmd_ack는 시뮬레이터가 직접 호출

MQTT 토픽:
  수신: /agv/cmd         서버 → AGV 명령
  송신: /agv/marker      AGV 위치/방향 보고 (marker_id + heading_deg)
        /agv/cmd_ack     명령 완료 보고

UART 패킷 (RPi 전용):
  [0xAA] [CMD] [LEN] [PAYLOAD...] [CRC]
  CRC = CMD ^ LEN ^ payload bytes

STM32 이벤트 → cmd_ack 매핑:
  ROTATE_DONE → cmd_ack (turn_left / turn_right / turn_180 중 마지막 전송한 것)
  LIFT_DONE(up)   → cmd_ack: lift_up
  LIFT_DONE(down) → cmd_ack: lift_down
"""

import json
import time
import threading
from typing import Callable, Optional

import paho.mqtt.client as mqtt

# ─── 설정 ───

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_CMD     = "/agv/cmd"
TOPIC_MARKER  = "/agv/marker"
TOPIC_CMD_ACK = "/agv/cmd_ack"

# UART 설정 (RPi 전용)
UART_PORT    = "/dev/ttyAMA0"
UART_BAUD    = 115200
UART_ENABLED = False  # True로 바꾸면 실제 UART 활성화

PACKET_HEAD = 0xAA


# ─── UART 명령 / 이벤트 코드 ───

class UartCmd:
    """RPi → STM32 명령"""
    MOVE_FORWARD = 0x01
    STOP         = 0x02
    LIFT_UP      = 0x03
    LIFT_DOWN    = 0x04
    ROTATE_LEFT  = 0x05
    ROTATE_RIGHT = 0x06
    ROTATE_180   = 0x07

class UartEvent:
    """STM32 → RPi 이벤트"""
    DONE = 0x81  # 동작 완료 (forward 제외 — ArUco로 감지)
    ACK  = 0xFF  # 명령 수신 확인


# ─── UART 유틸리티 ───

def _calc_crc(cmd: int, length: int, payload: bytes) -> int:
    crc = cmd ^ length
    for b in payload:
        crc ^= b
    return crc & 0xFF

def _build_packet(cmd: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    crc = _calc_crc(cmd, length, payload)
    return bytes([PACKET_HEAD, cmd, length]) + payload + bytes([crc])

def _parse_packet(data: bytes) -> Optional[dict]:
    if len(data) < 4 or data[0] != PACKET_HEAD:
        return None
    cmd    = data[1]
    length = data[2]
    if len(data) < 3 + length + 1:
        return None
    payload  = data[3:3 + length]
    crc_recv = data[3 + length]
    if crc_recv != _calc_crc(cmd, length, payload):
        return None
    return {"cmd": cmd, "payload": payload}


# ─── 메인 브릿지 ───

class Bridge:
    """
    AGV 명령 기반 브릿지 (단일 로봇)

    Parameters
    ----------
    rid : int
        로봇 ID (1 or 2)
    cmd_handler : callable, optional
        cmd_handler(rid: int, cmd: str) → None
        None이면 UART 모드 (실물 RPi)
        Isaac Sim: agv._on_cmd_from_bridge
    """

    def __init__(self, rid: int, cmd_handler: Optional[Callable] = None):
        self.rid = rid
        self._cmd_handler = cmd_handler

        self._client = mqtt.Client(client_id=f"bridge_{rid}_{int(time.time())}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # UART (RPi 전용)
        self._serial = None
        self._uart_thread: Optional[threading.Thread] = None
        self._running = False

        # 마지막으로 전송한 명령 추적 (DONE 수신 시 ack 타입 결정)
        self._last_cmd: Optional[str] = None

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    def connect(self):
        self._client.connect(MQTT_HOST, MQTT_PORT, 60)
        self._client.loop_start()

    def disconnect(self):
        self._running = False
        self._client.loop_stop()
        self._client.disconnect()
        if self._serial:
            self._serial.close()

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
                self._dispatch_cmd(cmd)

    def _dispatch_cmd(self, cmd: str):
        print(f"[Bridge-{self.rid}] <- cmd: {cmd}")
        if cmd != "forward":
            self._last_cmd = cmd  # DONE 수신 시 ack 타입 결정용
        if self._cmd_handler:
            # Isaac Sim 모드: 콜백 호출
            self._cmd_handler(self.rid, cmd)
        else:
            # RPi 모드: UART 전송
            self._uart_send(cmd)

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

    def publish_cmd_ack(self, cmd: str):
        """명령 완료 서버에 보고"""
        msg = {"type": "cmd_ack", "rid": self.rid, "cmd": cmd, "status": "done"}
        self._client.publish(TOPIC_CMD_ACK, json.dumps(msg))
        print(f"[Bridge-{self.rid}] -> /agv/cmd_ack  cmd={cmd}")

    # ─── UART 송신 (RPi 전용) ────────────────────────────────────────────────

    def _uart_send(self, cmd: str):
        """cmd 문자열 → UART 패킷 전송"""
        uart_map = {
            "forward":    (UartCmd.MOVE_FORWARD, b""),
            "turn_left":  (UartCmd.ROTATE_LEFT,  b""),
            "turn_right": (UartCmd.ROTATE_RIGHT, b""),
            "turn_180":   (UartCmd.ROTATE_180,   b""),
            "lift_up":    (UartCmd.LIFT_UP,       b""),
            "lift_down":  (UartCmd.LIFT_DOWN,     b""),
        }
        if cmd not in uart_map:
            print(f"[Bridge-{self.rid}] Unknown cmd: {cmd}")
            return
        uart_cmd, payload = uart_map[cmd]
        self._uart_write(_build_packet(uart_cmd, payload))

    def _uart_write(self, packet: bytes):
        if not UART_ENABLED or not self._serial:
            return
        try:
            self._serial.write(packet)
            self._serial.flush()
        except Exception as e:
            print(f"[Bridge-{self.rid}] UART write error: {e}")

    # ─── UART 수신 스레드 (RPi 전용) ──────────────────────────────────────────

    def _uart_read_loop(self):
        """STM32 이벤트 수신 → cmd_ack 발행"""
        buf = bytearray()
        while self._running:
            try:
                if self._serial and self._serial.in_waiting:
                    buf.extend(self._serial.read(self._serial.in_waiting))
                else:
                    time.sleep(0.005)
                    continue
            except Exception as e:
                print(f"[Bridge-{self.rid}] UART read error: {e}")
                time.sleep(0.1)
                continue

            while len(buf) >= 4:
                head_idx = buf.find(PACKET_HEAD)
                if head_idx < 0:
                    buf.clear()
                    break
                if head_idx > 0:
                    buf = buf[head_idx:]
                if len(buf) < 3:
                    break
                length    = buf[2]
                total_len = 3 + length + 1
                if len(buf) < total_len:
                    break
                parsed = _parse_packet(bytes(buf[:total_len]))
                buf    = buf[total_len:]
                if parsed:
                    self._handle_uart_event(parsed["cmd"], parsed["payload"])

    def _handle_uart_event(self, event: int, payload: bytes):
        """STM32 이벤트 → cmd_ack 발행"""
        if event == UartEvent.DONE:
            cmd = self._last_cmd or "turn_left"
            self._last_cmd = None
            self.publish_cmd_ack(cmd)

        elif event == UartEvent.ACK:
            pass  # 명령 수신 확인 (로깅 불필요)

    # ─── UART 초기화 ──────────────────────────────────────────────────────────

    def open_uart(self):
        """UART 포트 열기 + 수신 스레드 시작 (RPi 실물 모드)"""
        if not UART_ENABLED:
            print(f"[Bridge-{self.rid}] UART_ENABLED=False, 시뮬 모드로 동작")
            return
        try:
            import serial
            self._serial = serial.Serial(UART_PORT, UART_BAUD, timeout=0.1)
            self._running = True
            self._uart_thread = threading.Thread(
                target=self._uart_read_loop, daemon=True
            )
            self._uart_thread.start()
            print(f"[Bridge-{self.rid}] UART opened: {UART_PORT} @ {UART_BAUD}bps")
        except Exception as e:
            print(f"[Bridge-{self.rid}] UART open failed: {e}")


if __name__ == "__main__":
    import sys
    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    b = Bridge(rid)
    b.open_uart()
    b.connect()
    print(f"[Bridge-{rid}] 실행 중... Ctrl+C로 종료")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        b.disconnect()
