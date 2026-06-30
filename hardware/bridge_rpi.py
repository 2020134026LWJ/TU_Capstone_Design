"""
AGV Bridge (실물 RPi 전용) — 주원이 STM32 펌웨어 프로토콜 대응
TU Capstone Design - AGV 물류 피킹 시스템

  서버(MQTT) ↔ Bridge(RPi) ↔ STM32(UART)
  + 같은 라파의 카메라 스레드가 set_marker_offset()으로 최신 ArUco offset 공급
  ※ Isaac Sim(콜백) 버전은 isaac_simulation/bridge_isaac.py (이 파일은 실물 AGV 전용)

MQTT 토픽:
  수신: /agv/cmd         서버 → AGV 명령
  송신: /agv/marker      AGV 위치/방향 보고 (marker_id + heading)  ← 카메라 스레드가 호출
        /agv/cmd_ack     명령 완료 보고

UART 프로토콜 (RPi ↔ STM32, 주원이 rpi_uart.c / main.c 기준):
  송신 (Bridge→STM): ASCII "<command,±xxxx,±yyyy,±wwww>"  (21바이트)
      command = 1자리(1~7, 0=carrier/무명령), x/y/yaw = (mm·deg)×10 정수 → STM이 /10로 복원
      x/y/yaw offset = 카메라가 본 ArUco 오프셋 (set_marker_offset로 갱신한 최신값)
  수신 (STM→Bridge): 단일 바이트  0x81=DONE(동작완료) / 0xFF=ACK(명령수신)

  전송 흐름 (주원이 카메라와 동일 — bridge가 인수):
      set_marker_offset()가 매 프레임 패킷 스트리밍. command은 평소 0,
      MQTT 명령 수신 시 _pending_code에 실림 → EVT_ACK 오면 0으로 복귀.

[carrier 흐름 — 해결(2026-06-30)] 명령 계속 보내도 OK. 주원이가 STM에 DMA 처리 →
  명령은 필요할 때만 동작. bridge는 오프셋을 연속 스트리밍, command=0이 기본.

[미팅 후 조정 필요 — TODO]
  (2) forward 완료신호 : STM은 forward도 DONE을 보냄 ↔ 서버는 marker로 도착 인지 → 조율
  (3) 통신 유실 복구 : DONE 누락 시 timeout 주체(bridge/서버) + STM 상태조회 가능한가

[결정됨 — 카메라(비전)는 주원이 영역, bridge는 받기만]
  - UART 포트 = /dev/ttyAMA10 (주원이 카메라 코드와 동일)
  - 카메라가 (marker_id, x, y, yaw)를 계산 → set_marker_offset()/publish_marker()로 넘김
  - heading/yaw 변환·ArUco 비전 로직은 주원이 카메라가 담당 (bridge는 안 건드림)
"""

import json
import time
import threading
from typing import Optional

import paho.mqtt.client as mqtt

# ─── 설정은 hardware/config.py 에서 (배포 시 그 파일만 수정) ───
# 모듈 전역으로 가져옴 → SIL 테스트가 br.UART_ENABLED/br.UART_PORT 를 monkeypatch 가능
from hardware.config import (
    MQTT_HOST, MQTT_PORT,
    TOPIC_CMD, TOPIC_MARKER, TOPIC_CMD_ACK,
    UART_PORT, UART_BAUD, UART_ENABLED,
)


# ─── 명령 / 이벤트 코드 (주원이 rpi_uart.h와 동일) ───

# 명령 문자열 → STM 명령 코드 (ASCII 숫자 1자리로 전송)
CMD_CODE = {
    "forward":    1,
    "stop":       2,
    "lift_up":    3,
    "lift_down":  4,
    "turn_left":  5,
    "turn_right": 6,
    "turn_180":   7,
}

EVT_DONE = 0x81  # STM → 동작 완료
EVT_ACK  = 0xFF  # STM → 명령 수신 확인


class Bridge:
    """AGV UART 브릿지 (실물 RPi 전용, 단일 로봇)"""

    def __init__(self, rid: int):
        self.rid = rid

        self._client = mqtt.Client(client_id=f"bridge_{rid}_{int(time.time())}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # UART
        self._serial = None
        self._uart_thread: Optional[threading.Thread] = None
        self._running = False

        # 마지막으로 전송한 명령 추적 (DONE 수신 시 ack 타입 결정)
        self._last_cmd: Optional[str] = None

        # 현재 STM으로 실어 보내는 명령 코드 (0=carrier/무명령).
        # MQTT 명령 수신 시 set → EVT_ACK 오면 0으로 복귀 (주원이 카메라와 동일 흐름)
        self._pending_code = 0

        # 카메라 스레드가 갱신하는 최신 ArUco offset (x_mm, y_mm, yaw_deg)
        # → UART 송신 시 command과 합쳐 패킷에 실림
        self._latest_offset = (0.0, 0.0, 0.0)

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    def connect(self):
        self._client.connect(MQTT_HOST, MQTT_PORT, 60)
        self._client.loop_start()

    def disconnect(self):
        self._running = False
        if self._uart_thread:
            self._uart_thread.join(timeout=0.5)   # 수신 스레드 정리 후 포트 닫기
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
        code = CMD_CODE.get(cmd)
        if code is None:
            print(f"[Bridge-{self.rid}] Unknown cmd: {cmd}")
            return
        print(f"[Bridge-{self.rid}] <- cmd: {cmd}")
        if cmd != "forward":
            self._last_cmd = cmd      # DONE 수신 시 ack 타입 결정용
        self._pending_code = code     # arm: 다음 패킷부터 실려 나감 (EVT_ACK까지 반복)
        self._uart_send_packet()      # 즉시 1회도 송신 (마커 안 보여도 명령 전달 보장)

    # ─── 카메라 offset 공급 (같은 라파 카메라 스레드가 호출) ───────────────────

    def set_marker_offset(self, x_mm: float, y_mm: float, yaw_deg: float):
        """카메라 스레드가 최신 ArUco offset 갱신 → 즉시 STM으로 패킷 스트리밍.

        주원이 카메라가 매 프레임 UART에 쓰던 것을 bridge가 인수받음.
        command은 평소 0(carrier), MQTT 명령 수신 시에만 실림.
        같은 라파 안 메모리 공유라 MQTT 불필요. 단순 변수 대입(원자적)이라 락 불요.
        """
        self._latest_offset = (float(x_mm), float(y_mm), float(yaw_deg))
        self._uart_send_packet()

    # ─── 발행 ─────────────────────────────────────────────────────────────────

    def publish_marker(self, marker_id: int, heading_deg: Optional[int] = None):
        """ArUco 마커 감지 결과 서버에 보고 (카메라 스레드가 호출).

        옵션 a: heading_deg 미전달 → 서버가 경로 기반으로 heading 계산.
        (카메라 yaw는 마커 대비 회전 오프셋이지 절대방위가 아니므로 heading으로 안 보냄)
        """
        msg = {
            "rid": self.rid,
            "marker_id": marker_id,
            "ts": int(time.time()),
        }
        if heading_deg is not None:
            msg["heading"] = heading_deg
        self._client.publish(TOPIC_MARKER, json.dumps(msg))
        print(f"[Bridge-{self.rid}] -> /agv/marker  id={marker_id}")

    def publish_cmd_ack(self, cmd: str):
        """명령 완료 서버에 보고"""
        msg = {"type": "cmd_ack", "rid": self.rid, "cmd": cmd, "status": "done"}
        self._client.publish(TOPIC_CMD_ACK, json.dumps(msg))
        print(f"[Bridge-{self.rid}] -> /agv/cmd_ack  cmd={cmd}")

    # ─── UART 송신 (ASCII <cmd,x,y,yaw>) ──────────────────────────────────────

    def _uart_send_packet(self):
        """현재 armed 명령(_pending_code) + 최신 카메라 offset → 주원이 ASCII 패킷 전송.

        set_marker_offset(프레임마다) + _dispatch_cmd(명령 시)에서 호출.
        _pending_code=0이면 carrier 패킷(오프셋만, STM PID 정렬용).
        """
        x, y, yaw = self._latest_offset
        # 주원이 포맷: <command,±xxxx,±yyyy,±wwww>  (offset은 ×10 정수, STM이 /10 복원)
        msg = f"<{self._pending_code},{int(x * 10):+05d},{int(y * 10):+05d},{int(yaw * 10):+05d}>"
        self._uart_write(msg.encode())

    def _uart_write(self, data: bytes):
        if not UART_ENABLED or not self._serial:
            return
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as e:
            print(f"[Bridge-{self.rid}] UART write error: {e}")

    # ─── UART 수신 스레드 (단일 바이트 이벤트) ────────────────────────────────

    def _uart_read_loop(self):
        """STM32 이벤트(단일 바이트) 수신 → cmd_ack 발행"""
        while self._running:
            try:
                b = self._serial.read(1) if self._serial else b""  # timeout=0.1로 블록 방지
            except Exception as e:
                print(f"[Bridge-{self.rid}] UART read error: {e}")
                time.sleep(0.1)
                continue
            if not b:
                continue
            self._handle_uart_event(b[0])

    def _handle_uart_event(self, event: int):
        """STM32 단일 바이트 이벤트 → cmd_ack 발행"""
        if event == EVT_DONE:
            # forward 완료는 카메라 마커가 서버에 보고함(_marker_mixin이 forward의 ACK로 사용).
            # → forward DONE은 서버로 보내지 않음 (중복 + 다음 in_flight 명령 오ack 방지, 문제 #1).
            #   forward는 _dispatch_cmd에서 _last_cmd를 안 set하므로 None → 여기서 자동 스킵.
            #   turn/lift만 _last_cmd가 있어 cmd_ack 발행.
            cmd = self._last_cmd
            self._last_cmd = None
            if cmd is not None:
                self.publish_cmd_ack(cmd)
        elif event == EVT_ACK:
            # STM이 명령 수신 확인 → carrier(0)로 복귀 (다음 패킷부터 오프셋만 스트리밍)
            self._pending_code = 0

    # ─── UART 초기화 ──────────────────────────────────────────────────────────

    def open_uart(self):
        """UART 포트 열기 + 수신 스레드 시작"""
        if not UART_ENABLED:
            print(f"[Bridge-{self.rid}] UART_ENABLED=False, UART 비활성 모드")
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
