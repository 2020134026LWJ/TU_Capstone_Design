"""
AGV Bridge - RPi 양방향 중계 모듈
TU Capstone Design - AGV 물류 피킹 시스템

역할:
  서버(MQTT) ↔ Bridge(RPi) ↔ STM32(UART)

수신:
  - MQTT /agv/plan       : 서버로부터 경로 계획 수신
  - MQTT /agv/shelf_cmd  : 서버로부터 선반 리프트 명령 수신
  - UART STM32           : 이동완료, 리프트완료, 마커통과 등 이벤트 수신

송신:
  - UART STM32           : 이동명령, 리프트명령 전달
  - MQTT /agv/state      : 로봇 상태를 서버에 보고
  - MQTT /agv/arrived    : 로봇 도착 이벤트를 서버에 보고

UART 패킷 프로토콜:
  [0xAA] [CMD] [LEN] [PAYLOAD...] [CRC]
  CRC = CMD ^ LEN ^ payload[0] ^ payload[1] ^ ...
"""

import json
import time
import struct
import threading
from enum import IntEnum
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

# ─── 설정 ───

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_PLAN = "/agv/plan"
TOPIC_SHELF_CMD = "/agv/shelf_cmd"
TOPIC_LOWCMD = "/agv/lowcmd"
TOPIC_STATE = "/agv/state"
TOPIC_ARRIVED = "/agv/arrived"

# UART 설정 (실제 하드웨어에서 사용)
UART_PORT = "/dev/ttyAMA0"   # RPi UART
UART_BAUD = 115200
UART_ENABLED = False          # True로 바꾸면 실제 UART 활성화

PACKET_HEAD = 0xAA


# ─── UART 명령 코드 ───

class UartCmd(IntEnum):
    """RPi → STM32 명령"""
    MOVE_TO_NODE = 0x01
    STOP = 0x02
    LIFT_UP = 0x03
    LIFT_DOWN = 0x04
    SET_SPEED = 0x05
    REQUEST_STATUS = 0x06
    ROTATE = 0x07


class UartEvent(IntEnum):
    """STM32 → RPi 이벤트"""
    MOVE_DONE = 0x81
    MOVE_FAILED = 0x82
    LIFT_DONE = 0x83
    LIFT_FAILED = 0x84
    MARKER_PASSED = 0x85
    STATUS_REPORT = 0x86
    ROTATE_DONE = 0x87
    OBSTACLE_DETECTED = 0x88
    ACK = 0xFF


# ─── UART 패킷 유틸리티 ───

def calc_crc(cmd: int, length: int, payload: bytes) -> int:
    """CRC 계산: CMD ^ LEN ^ payload bytes"""
    crc = cmd ^ length
    for b in payload:
        crc ^= b
    return crc & 0xFF


def build_packet(cmd: int, payload: bytes = b"") -> bytes:
    """UART 패킷 생성"""
    length = len(payload)
    crc = calc_crc(cmd, length, payload)
    return bytes([PACKET_HEAD, cmd, length]) + payload + bytes([crc])


def parse_packet(data: bytes) -> Optional[Dict[str, Any]]:
    """
    UART 패킷 파싱
    Returns: {"cmd": int, "payload": bytes} or None
    """
    if len(data) < 4 or data[0] != PACKET_HEAD:
        return None

    cmd = data[1]
    length = data[2]

    if len(data) < 3 + length + 1:
        return None

    payload = data[3:3 + length]
    crc_recv = data[3 + length]
    crc_calc = calc_crc(cmd, length, payload)

    if crc_recv != crc_calc:
        print(f"[UART] CRC mismatch: recv=0x{crc_recv:02X}, calc=0x{crc_calc:02X}")
        return None

    return {"cmd": cmd, "payload": payload}


# ─── 개별 로봇 상태 ───

class RobotState:
    """개별 로봇 상태"""
    def __init__(self, rid: int):
        self.rid = rid
        self.path: List[int] = []       # 노드 경로
        self.idx: int = 0               # 현재 경로 인덱스
        self.speed: float = 0.3
        self.current_node: Optional[int] = None
        self.done: bool = False
        self.lift_state: str = "down"   # "up" / "down"
        self.pending_lift: Optional[str] = None  # "up" / "down" 대기 중


# ─── 메인 브릿지 ───

class Bridge:
    """
    양방향 브릿지: MQTT ↔ UART

    서버 → Bridge → STM32:
      MQTT plan/shelf_cmd 수신 → UART 명령 전송

    STM32 → Bridge → 서버:
      UART 이벤트 수신 → MQTT state/arrived 발행
    """

    def __init__(self, num_robots: int = 2):
        self.num_robots = num_robots
        self.robots: Dict[int, RobotState] = {}
        for rid in range(1, num_robots + 1):
            self.robots[rid] = RobotState(rid)

        # MQTT
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message

        # UART (실제 하드웨어)
        self.serial_port = None
        self.uart_thread: Optional[threading.Thread] = None
        self.running = False

    # ═══════════════════════════════════════════════
    #  MQTT 콜백
    # ═══════════════════════════════════════════════

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        print(f"[Bridge] MQTT connected, rc={rc}")
        client.subscribe(TOPIC_PLAN)
        client.subscribe(TOPIC_SHELF_CMD)
        client.subscribe(TOPIC_STATE)
        print(f"[Bridge] Subscribed: {TOPIC_PLAN}, {TOPIC_SHELF_CMD}, {TOPIC_STATE}")

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[Bridge] JSON decode error on {msg.topic}: {e}")
            return

        if msg.topic == TOPIC_PLAN:
            self._handle_plan(payload)
        elif msg.topic == TOPIC_SHELF_CMD:
            self._handle_shelf_cmd(payload)
        elif msg.topic == TOPIC_STATE:
            self._handle_state(payload)

    # ═══════════════════════════════════════════════
    #  MQTT → STM32 (서버 명령 수신 → UART 전달)
    # ═══════════════════════════════════════════════

    def _handle_plan(self, plan: Dict[str, Any]) -> None:
        """서버로부터 경로 계획 수신 → 첫 번째 이동 명령 전송"""
        speed = float(plan.get("speed", 0.3))

        for robot_plan in plan.get("robots", []):
            rid = int(robot_plan.get("rid", -1))
            if rid not in self.robots:
                continue

            robot = self.robots[rid]
            robot.path = robot_plan.get("node_path", [])
            robot.speed = speed
            robot.idx = 1 if len(robot.path) > 1 else 0
            robot.current_node = robot.path[0] if robot.path else None
            robot.done = False

            print(f"[Bridge] AGV {rid}: path={robot.path}, speed={speed}")

            # 첫 번째 이동 명령 전송
            if robot.path and robot.idx < len(robot.path):
                self._send_move_cmd(rid, robot.path[robot.idx], speed)

    def _handle_shelf_cmd(self, data: Dict[str, Any]) -> None:
        """서버로부터 선반 리프트 명령 수신 → UART 전달"""
        rid = int(data.get("rid", -1))
        command = data.get("command", "")
        shelf_id = data.get("shelf_id")

        if rid not in self.robots:
            return

        robot = self.robots[rid]

        if command == "pickup":
            robot.pending_lift = "up"
            self._send_uart(rid, UartCmd.LIFT_UP)
            print(f"[Bridge] AGV {rid}: LIFT_UP for shelf {shelf_id}")

        elif command == "putdown":
            robot.pending_lift = "down"
            self._send_uart(rid, UartCmd.LIFT_DOWN)
            print(f"[Bridge] AGV {rid}: LIFT_DOWN for shelf {shelf_id}")

    def _handle_state(self, state: Dict[str, Any]) -> None:
        """
        Webots 시뮬레이션용: /agv/state로 들어오는 위치 업데이트
        (실제 하드웨어에서는 UART로 대체됨)
        """
        rid = state.get("rid")
        if rid is None or rid not in self.robots:
            return

        robot = self.robots[rid]
        robot.current_node = state.get("current_node", robot.current_node)

        # 목표 노드 도달 체크
        if robot.path and robot.idx < len(robot.path):
            target = robot.path[robot.idx]
            if robot.current_node == target:
                self._on_node_reached(rid, target)

    # ═══════════════════════════════════════════════
    #  STM32 → MQTT (UART 이벤트 수신 → 서버 보고)
    # ═══════════════════════════════════════════════

    def _handle_uart_event(self, rid: int, cmd: int, payload: bytes) -> None:
        """STM32에서 수신한 UART 이벤트 처리"""
        robot = self.robots.get(rid)
        if not robot:
            return

        if cmd == UartEvent.MOVE_DONE:
            node_id = payload[0] if payload else 0
            print(f"[Bridge] AGV {rid}: MOVE_DONE at node {node_id}")
            robot.current_node = node_id
            self._on_node_reached(rid, node_id)

        elif cmd == UartEvent.MOVE_FAILED:
            error_code = payload[0] if payload else 0
            print(f"[Bridge] AGV {rid}: MOVE_FAILED, error={error_code}")
            self._publish_state(rid, "error")

        elif cmd == UartEvent.LIFT_DONE:
            lift_state = "up" if (payload and payload[0] == 1) else "down"
            robot.lift_state = lift_state
            robot.pending_lift = None
            print(f"[Bridge] AGV {rid}: LIFT_DONE, state={lift_state}")
            self._publish_state(rid, "lift_done")

            # 리프트 완료 후 경로가 남아있으면 다음 이동 시작
            if robot.path and robot.idx < len(robot.path) and not robot.done:
                self._send_move_cmd(rid, robot.path[robot.idx], robot.speed)

        elif cmd == UartEvent.LIFT_FAILED:
            error_code = payload[0] if payload else 0
            print(f"[Bridge] AGV {rid}: LIFT_FAILED, error={error_code}")
            self._publish_state(rid, "error")

        elif cmd == UartEvent.MARKER_PASSED:
            node_id = payload[0] if payload else 0
            robot.current_node = node_id
            print(f"[Bridge] AGV {rid}: MARKER_PASSED node {node_id}")
            self._publish_state(rid, "moving")

        elif cmd == UartEvent.STATUS_REPORT:
            self._parse_status_report(rid, payload)

        elif cmd == UartEvent.ROTATE_DONE:
            print(f"[Bridge] AGV {rid}: ROTATE_DONE")

        elif cmd == UartEvent.OBSTACLE_DETECTED:
            distance = payload[0] if payload else 0
            print(f"[Bridge] AGV {rid}: OBSTACLE at {distance}cm")

        elif cmd == UartEvent.ACK:
            cmd_echo = payload[0] if payload else 0
            print(f"[Bridge] AGV {rid}: ACK for cmd 0x{cmd_echo:02X}")

    def _on_node_reached(self, rid: int, node_id: int) -> None:
        """노드 도착 처리 (UART/시뮬레이션 공통)"""
        robot = self.robots.get(rid)
        if not robot:
            return

        robot.current_node = node_id

        # 경로상 다음 노드로 진행
        if robot.path and robot.idx < len(robot.path):
            target = robot.path[robot.idx]
            if node_id == target:
                robot.idx += 1
                print(f"[Bridge] AGV {rid}: reached node {node_id}, next idx={robot.idx}")

                if robot.idx >= len(robot.path):
                    # 경로 완료 → 서버에 도착 보고
                    robot.done = True
                    print(f"[Bridge] AGV {rid}: PATH COMPLETED at node {node_id}")
                    self._publish_arrived(rid, node_id)
                else:
                    # 다음 노드로 이동 명령
                    next_target = robot.path[robot.idx]
                    self._send_move_cmd(rid, next_target, robot.speed)
                    self._publish_state(rid, "moving")

    # ═══════════════════════════════════════════════
    #  MQTT 발행 (서버에 보고)
    # ═══════════════════════════════════════════════

    def _publish_state(self, rid: int, state_str: str) -> None:
        """로봇 상태를 서버에 보고"""
        robot = self.robots.get(rid)
        if not robot:
            return

        msg = {
            "rid": rid,
            "current_node": robot.current_node,
            "state": state_str,
            "lift_state": robot.lift_state,
            "path_idx": robot.idx,
            "path_length": len(robot.path),
            "done": robot.done,
            "ts": time.time(),
        }
        self.mqtt_client.publish(TOPIC_STATE, json.dumps(msg), qos=0)

    def _publish_arrived(self, rid: int, node_id: int) -> None:
        """로봇 도착 이벤트를 서버에 보고 → request_handler.robot_arrived 트리거"""
        msg = {
            "type": "robot_arrived",
            "rid": rid,
            "node": node_id,
            "ts": time.time(),
        }
        self.mqtt_client.publish(TOPIC_ARRIVED, json.dumps(msg), qos=0)
        print(f"[Bridge] AGV {rid}: published arrived at node {node_id}")

    # ═══════════════════════════════════════════════
    #  UART 송신 (STM32에 명령)
    # ═══════════════════════════════════════════════

    def _send_move_cmd(self, rid: int, target_node: int, speed: float) -> None:
        """이동 명령 → STM32 (UART) + Webots 호환 (MQTT)"""
        speed_int = int(speed * 1000)  # mm/s
        speed_hi = (speed_int >> 8) & 0xFF
        speed_lo = speed_int & 0xFF

        # UART 전송
        self._send_uart(rid, UartCmd.MOVE_TO_NODE,
                        bytes([target_node, speed_hi, speed_lo]))

        # Webots 시뮬레이션 호환: MQTT lowcmd도 발행
        lowcmd = {
            "rid": rid,
            "v": float(speed),
            "w": 0.0,
            "target_node": int(target_node),
        }
        self.mqtt_client.publish(TOPIC_LOWCMD, json.dumps(lowcmd), qos=0)
        print(f"[Bridge] AGV {rid}: MOVE -> node {target_node}, speed={speed}")

    def _send_uart(self, rid: int, cmd: UartCmd, payload: bytes = b"") -> None:
        """UART 패킷 전송 (실제 하드웨어에서만)"""
        if not UART_ENABLED or not self.serial_port:
            return

        # rid를 payload 앞에 추가 (STM32가 자기 것인지 확인)
        full_payload = bytes([rid]) + payload
        packet = build_packet(cmd, full_payload)

        try:
            self.serial_port.write(packet)
            self.serial_port.flush()
        except Exception as e:
            print(f"[UART] Send error: {e}")

    # ═══════════════════════════════════════════════
    #  UART 수신 스레드
    # ═══════════════════════════════════════════════

    def _uart_read_thread(self) -> None:
        """UART 수신 루프 (별도 스레드)"""
        buf = bytearray()

        while self.running:
            try:
                if self.serial_port and self.serial_port.in_waiting:
                    buf.extend(self.serial_port.read(self.serial_port.in_waiting))
                else:
                    time.sleep(0.01)
                    continue
            except Exception as e:
                print(f"[UART] Read error: {e}")
                time.sleep(0.1)
                continue

            # 패킷 파싱 시도
            while len(buf) >= 4:
                # HEAD 찾기
                head_idx = buf.find(PACKET_HEAD)
                if head_idx < 0:
                    buf.clear()
                    break
                if head_idx > 0:
                    buf = buf[head_idx:]

                if len(buf) < 3:
                    break

                cmd = buf[1]
                length = buf[2]
                total_len = 3 + length + 1  # HEAD + CMD + LEN + payload + CRC

                if len(buf) < total_len:
                    break

                packet_data = bytes(buf[:total_len])
                buf = buf[total_len:]

                parsed = parse_packet(packet_data)
                if parsed:
                    # payload 첫 바이트 = rid
                    p = parsed["payload"]
                    if len(p) >= 1:
                        rid = p[0]
                        event_payload = p[1:]
                        self._handle_uart_event(rid, parsed["cmd"], event_payload)

    def _parse_status_report(self, rid: int, payload: bytes) -> None:
        """STM32 상태 보고 파싱"""
        if len(payload) < 7:
            return

        state = payload[0]
        node = payload[1]
        lift = payload[2]
        speed = (payload[3] << 8) | payload[4]
        imu_heading = (payload[5] << 8) | payload[6]

        robot = self.robots.get(rid)
        if robot:
            robot.current_node = node
            robot.lift_state = "up" if lift == 1 else "down"

        print(f"[Bridge] AGV {rid}: STATUS state={state}, node={node}, "
              f"lift={'up' if lift else 'down'}, speed={speed}mm/s, heading={imu_heading}")

    # ═══════════════════════════════════════════════
    #  Webots 시뮬레이션 호환: 주기적 명령 발행
    # ═══════════════════════════════════════════════

    def tick(self) -> None:
        """
        주기적 호출 (1Hz)
        Webots 시뮬레이션에서 lowcmd를 주기적으로 재발행
        실제 하드웨어에서는 UART 이벤트 기반이므로 tick 불필요
        """
        if UART_ENABLED:
            return  # 실제 HW에서는 이벤트 기반

        for rid, robot in self.robots.items():
            if not robot.path or robot.done:
                continue

            target_node = robot.path[min(robot.idx, len(robot.path) - 1)]
            v = robot.speed

            lowcmd = {
                "rid": rid,
                "v": float(v),
                "w": 0.0,
                "target_node": int(target_node),
            }
            self.mqtt_client.publish(TOPIC_LOWCMD, json.dumps(lowcmd), qos=0)

    # ═══════════════════════════════════════════════
    #  실행
    # ═══════════════════════════════════════════════

    def run(self) -> None:
        """메인 루프"""
        self.running = True

        # MQTT 연결
        self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.mqtt_client.loop_start()

        # UART 초기화 (하드웨어 모드)
        if UART_ENABLED:
            try:
                import serial
                self.serial_port = serial.Serial(
                    UART_PORT, UART_BAUD,
                    timeout=0.1,
                )
                print(f"[Bridge] UART opened: {UART_PORT} @ {UART_BAUD}bps")

                self.uart_thread = threading.Thread(
                    target=self._uart_read_thread, daemon=True
                )
                self.uart_thread.start()
            except Exception as e:
                print(f"[Bridge] UART open failed: {e}")
                print("[Bridge] Falling back to simulation mode")

        print(f"[Bridge] Running with {self.num_robots} robots "
              f"({'UART+MQTT' if UART_ENABLED else 'MQTT only (simulation)'})")

        try:
            while self.running:
                self.tick()
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[Bridge] Exit")
        finally:
            self.running = False
            if self.serial_port:
                self.serial_port.close()
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


if __name__ == "__main__":
    import sys
    num_robots = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    Bridge(num_robots=num_robots).run()
