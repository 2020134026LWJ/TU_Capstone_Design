"""
AGV MQTT Controller for Webots Simulation (7x7 Grid)
- /agv/plan 구독 → 다중 노드 경로 추종
- /agv/arrived 발행 → 목표 도착 알림
- /agv/shelf_cmd 구독 → 선반 리프트 제어
"""

import json
import math
import sys
import time
import os

controller_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, controller_dir)

from controller import Supervisor

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError as e:
    print(f"[AGV] paho-mqtt not available: {e}")
    MQTT_AVAILABLE = False

# ─── 설정 ───
MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_PLAN = "/agv/plan"
TOPIC_STATE = "/agv/state"
TOPIC_ARRIVED = "/agv/arrived"
TOPIC_SHELF_CMD = "/agv/shelf_cmd"
TOPIC_SHELF_ACK = "/agv/shelf_ack"

CELL_SIZE = 1.0
GRID_COLS = 8
GRID_ROWS = 4

MOVE_SPEED = 3.0
TURN_SPEED = 2.0
POSITION_TOLERANCE = 0.05
ANGLE_TOLERANCE = 0.03

DIR_EAST = 0.0
DIR_NORTH = math.pi / 2
DIR_WEST = math.pi
DIR_SOUTH = -math.pi / 2


class AGVController:
    """디퍼렌셜 드라이브 AGV 컨트롤러 (Supervisor)"""

    def __init__(self):
        self.robot = Supervisor()
        self.timestep = int(self.robot.getBasicTimeStep())

        self.rid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
        self.start_node = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        print(f"[AGV {self.rid}] Init - start node: {self.start_node}")

        # ─── 센서 ───
        self.gps = self.robot.getDevice("gps")
        self.gps.enable(self.timestep)
        self.compass = self.robot.getDevice("compass")
        self.compass.enable(self.timestep)

        # ─── 모터 (Pioneer 3-DX) ───
        self.left_motor = self.robot.getDevice("left wheel")
        self.right_motor = self.robot.getDevice("right wheel")
        self.left_motor.setPosition(float('inf'))
        self.right_motor.setPosition(float('inf'))
        self.left_motor.setVelocity(0.0)
        self.right_motor.setVelocity(0.0)

        # ─── 리프트 ───
        self.lift_motor = self.robot.getDevice("lift_motor")
        self.lift_sensor = self.robot.getDevice("lift_sensor")
        self.lift_sensor.enable(self.timestep)

        # ─── 상태 ───
        self.current_node = self.start_node
        self.state = "IDLE"
        self.target_angle = 0.0
        self.target_pos = self.node_to_world(self.start_node)
        self.move_start_pos = self.target_pos
        self._moving_to_node = self.start_node

        # ─── 경로 추종 ───
        self.path_queue = []
        self.goal_node = None

        # ─── 선반 ───
        self.carrying_shelf = None

        # ─── MQTT ───
        self.mqtt_client = None
        self.mqtt_connected = False
        if MQTT_AVAILABLE:
            self._setup_mqtt()

    # ─── 좌표 변환 ───

    def node_to_world(self, node_id):
        """Node ID → 월드 좌표 (4x8 grid + workstation 33,34)"""
        if node_id == 33:  # W1
            return -0.5, 0.5
        elif node_id == 34:  # W2
            return -0.5, 3.5
        idx = node_id - 1
        col = idx % GRID_COLS
        row = idx // GRID_COLS
        return (col + 0.5) * CELL_SIZE, (row + 0.5) * CELL_SIZE

    # ─── MQTT ───

    def _setup_mqtt(self):
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_message = self._on_message
        try:
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            print(f"[AGV {self.rid}] MQTT connecting...")
        except Exception as e:
            print(f"[AGV {self.rid}] MQTT connection failed: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        self.mqtt_connected = True
        client.subscribe(TOPIC_PLAN)
        client.subscribe(TOPIC_SHELF_CMD)
        print(f"[AGV {self.rid}] MQTT connected, subscribed: {TOPIC_PLAN}, {TOPIC_SHELF_CMD}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return

        if msg.topic == TOPIC_PLAN:
            self._handle_plan(data)
        elif msg.topic == TOPIC_SHELF_CMD:
            self._handle_shelf_cmd(data)

    def _handle_plan(self, data):
        """경로 계획 수신 → 경로 추종 시작"""
        for r in data.get("robots", []):
            if int(r.get("rid", -1)) != self.rid:
                continue

            node_path = r.get("node_path", [])
            goal = r.get("goal")

            if node_path and len(node_path) > 1:
                self.path_queue = list(node_path[1:])  # 첫 노드(현재 위치) 제외
                self.goal_node = goal
                print(f"[AGV {self.rid}] Plan received: {node_path} → goal {goal}")
                self._move_to_next_node()
            elif goal is not None:
                # 이미 목표 위치에 있음
                self.goal_node = goal
                self.current_node = goal
                print(f"[AGV {self.rid}] Already at goal {goal}")
                self._publish_arrived(goal)
            break

    def _handle_shelf_cmd(self, data):
        """선반 리프트 명령 처리"""
        if int(data.get("rid", -1)) != self.rid:
            return

        command = data.get("command")
        shelf_id = data.get("shelf_id")
        print(f"[AGV {self.rid}] Shelf cmd: {command} shelf {shelf_id}")

        if command == "pickup":
            self.lift_motor.setPosition(0.10)
            self.carrying_shelf = shelf_id
            self._publish_shelf_ack(shelf_id, "pickup")
        elif command == "putdown":
            self.lift_motor.setPosition(0.0)
            if self.carrying_shelf:
                self._detach_shelf(self.carrying_shelf)
            self.carrying_shelf = None
            self._publish_shelf_ack(shelf_id, "putdown")

    # ─── 선반 Supervisor 제어 ───

    def _move_shelf_to_robot(self, shelf_id):
        """Supervisor: 선반을 로봇 위치로 이동"""
        shelf_node = self.robot.getFromDef(f"SHELF_{shelf_id}")
        if shelf_node:
            x, y = self.get_position()
            shelf_node.getField("translation").setSFVec3f([x, y, 0.12])

    def _detach_shelf(self, shelf_id):
        """Supervisor: 선반을 현재 위치에 내려놓기"""
        shelf_node = self.robot.getFromDef(f"SHELF_{shelf_id}")
        if shelf_node:
            x, y = self.get_position()
            shelf_node.getField("translation").setSFVec3f([x, y, 0])

    # ─── 경로 추종 ───

    def _move_to_next_node(self):
        """경로 큐에서 다음 노드로 이동"""
        if not self.path_queue:
            return
        next_node = self.path_queue.pop(0)
        self._set_target(next_node)

    def _set_target(self, node):
        """단일 노드 이동 목표 설정"""
        self._moving_to_node = node
        self.target_pos = self.node_to_world(node)

        cx, cy = self.get_position()
        self.move_start_pos = (cx, cy)

        dx = self.target_pos[0] - cx
        dy = self.target_pos[1] - cy

        if abs(dx) < 0.01 and abs(dy) < 0.01:
            self._on_node_reached(node)
            return

        if abs(dx) > abs(dy):
            self.target_angle = DIR_EAST if dx > 0 else DIR_WEST
        else:
            self.target_angle = DIR_NORTH if dy > 0 else DIR_SOUTH

        self.state = "TURNING"

    def _on_node_reached(self, node):
        """노드 도착 처리"""
        self.current_node = node
        print(f"[AGV {self.rid}] Reached node {node}")

        if self.path_queue:
            self._move_to_next_node()
        else:
            self.state = "IDLE"
            self.stop()
            if self.goal_node is not None:
                self._publish_arrived(node)
                self.goal_node = None

    # ─── MQTT 발행 ───

    def _publish_arrived(self, node):
        if not self.mqtt_connected:
            return
        msg = {"type": "robot_arrived", "rid": self.rid, "node": node}
        self.mqtt_client.publish(TOPIC_ARRIVED, json.dumps(msg))
        print(f"[AGV {self.rid}] → arrived at node {node}")

    def _publish_shelf_ack(self, shelf_id, command):
        if not self.mqtt_connected:
            return
        msg = {"rid": self.rid, "command": command, "shelf_id": shelf_id, "status": "done"}
        self.mqtt_client.publish(TOPIC_SHELF_ACK, json.dumps(msg))
        print(f"[AGV {self.rid}] → shelf_ack: {command} shelf {shelf_id}")

    def publish_state(self):
        if not self.mqtt_connected:
            return
        state = {
            "rid": self.rid,
            "current_node": self.current_node,
            "state": self.state,
            "carrying_shelf": self.carrying_shelf,
            "path_remaining": len(self.path_queue),
            "ts": int(time.time()),
        }
        try:
            self.mqtt_client.publish(TOPIC_STATE, json.dumps(state), qos=0)
        except Exception:
            pass

    # ─── 센서 ───

    def get_position(self):
        values = self.gps.getValues()
        return values[0], values[1]

    def get_bearing(self):
        values = self.compass.getValues()
        return math.atan2(values[0], values[1])

    def normalize_angle(self, angle):
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    # ─── 모터 ───

    def set_motors(self, left, right):
        self.left_motor.setVelocity(left)
        self.right_motor.setVelocity(right)

    def stop(self):
        self.set_motors(0.0, 0.0)

    # ─── 상태 머신 ───

    def update(self):
        if self.state == "IDLE":
            self.stop()

        elif self.state == "TURNING":
            current_angle = self.get_bearing()
            angle_diff = self.normalize_angle(self.target_angle - current_angle)

            if abs(angle_diff) < ANGLE_TOLERANCE:
                self.stop()
                self.state = "MOVING"
                cx, cy = self.get_position()
                self.move_start_pos = (cx, cy)
            else:
                speed = TURN_SPEED if angle_diff > 0 else -TURN_SPEED
                speed *= min(1.0, abs(angle_diff) / 0.5)
                self.set_motors(-speed, speed)

        elif self.state == "MOVING":
            cx, cy = self.get_position()
            tx, ty = self.target_pos
            dx, dy = tx - cx, ty - cy
            distance = math.sqrt(dx * dx + dy * dy)

            if distance < POSITION_TOLERANCE:
                self.stop()
                self._on_node_reached(self._moving_to_node)
            else:
                current_angle = self.get_bearing()
                angle_to_target = math.atan2(dy, dx)
                angle_diff = self.normalize_angle(angle_to_target - current_angle)
                correction = max(-1.0, min(1.0, angle_diff * 2.0))
                self.set_motors(MOVE_SPEED - correction, MOVE_SPEED + correction)

    # ─── 메인 루프 ───

    def run(self):
        print(f"[AGV {self.rid}] Started at node {self.start_node}")

        for _ in range(10):
            self.robot.step(self.timestep)

        counter = 0
        while self.robot.step(self.timestep) != -1:
            self.update()

            # 선반 운반 중이면 위치 동기화
            if self.carrying_shelf is not None:
                self._move_shelf_to_robot(self.carrying_shelf)

            counter += 1
            if counter >= 6:
                self.publish_state()
                counter = 0

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


if __name__ == "__main__":
    controller = AGVController()
    controller.run()
