"""
AGV MQTT Controller for Webots Simulation
TU Capstone Design - AGV Logistics Picking System

Differential Drive AGV Controller
- Grid-based 90-degree movement
- MQTT communication with bridge.py
"""

import json
import math
import sys
import time

# 컨트롤러 디렉토리를 path에 추가 (로컬 paho 패키지 사용)
import os
controller_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, controller_dir)

from controller import Robot

# MQTT 라이브러리
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
    print("[AGV] paho-mqtt loaded successfully")
except ImportError as e:
    print(f"[AGV] WARNING: paho-mqtt not installed. Error: {e}")
    MQTT_AVAILABLE = False


# ============================================================
# 설정
# ============================================================
MQTT_HOST = "localhost"
MQTT_PORT = 1883
TOPIC_LOWCMD = "/agv/lowcmd"
TOPIC_STATE = "/agv/state"

CELL_SIZE = 1.0  # 그리드 한 칸 크기 (미터)
GRID_COLS = 9
GRID_ROWS = 5

# 이동 파라미터
MAX_SPEED = 6.28  # 최대 휠 속도 (rad/s)
MOVE_SPEED = 3.0  # 이동 시 휠 속도
TURN_SPEED = 2.0  # 회전 시 휠 속도

POSITION_TOLERANCE = 0.05  # 위치 도달 허용 오차 (m)
ANGLE_TOLERANCE = 0.03  # 각도 도달 허용 오차 (rad, 약 1.7도)

# 방향 정의 (라디안)
DIR_EAST = 0.0           # +X 방향 (오른쪽)
DIR_NORTH = math.pi/2    # +Y 방향 (위)
DIR_WEST = math.pi       # -X 방향 (왼쪽)
DIR_SOUTH = -math.pi/2   # -Y 방향 (아래)


class AGVController:
    """디퍼렌셜 드라이브 AGV 컨트롤러"""

    def __init__(self):
        self.robot = Robot()
        self.timestep = int(self.robot.getBasicTimeStep())

        # 컨트롤러 인자: [rid, start_node]
        self.rid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
        self.start_node = int(sys.argv[2]) if len(sys.argv) > 2 else 1

        print(f"[AGV {self.rid}] Init - start node: {self.start_node}")

        # 센서 초기화
        self.gps = self.robot.getDevice("gps")
        self.gps.enable(self.timestep)

        self.compass = self.robot.getDevice("compass")
        self.compass.enable(self.timestep)

        # 디퍼렌셜 드라이브 휠 모터 (Pioneer 3-DX 기준)
        self.left_motor = self.robot.getDevice("left wheel")
        self.right_motor = self.robot.getDevice("right wheel")

        self.left_motor.setPosition(float('inf'))
        self.right_motor.setPosition(float('inf'))
        self.left_motor.setVelocity(0.0)
        self.right_motor.setVelocity(0.0)

        # 상태 변수
        self.current_node = self.start_node
        self.target_node = self.start_node
        self.speed = 0.0
        self.progress = 0.0

        # 이동 상태 머신
        self.state = "IDLE"  # IDLE, TURNING, MOVING, ARRIVED
        self.target_angle = 0.0
        self.target_pos = self.node_to_world(self.start_node)
        self.move_start_pos = self.target_pos

        # MQTT
        self.mqtt_client = None
        self.mqtt_connected = False
        if MQTT_AVAILABLE:
            self._setup_mqtt()

    def node_to_world(self, node_id):
        """Node ID → 월드 좌표"""
        idx = node_id - 1
        col = idx % GRID_COLS
        row = idx // GRID_COLS
        world_x = (col + 0.5) * CELL_SIZE
        world_y = (row + 0.5) * CELL_SIZE
        return world_x, world_y

    def world_to_node(self, x, y):
        """월드 좌표 → Node ID"""
        col = int(x / CELL_SIZE)
        row = int(y / CELL_SIZE)
        col = max(0, min(GRID_COLS - 1, col))
        row = max(0, min(GRID_ROWS - 1, row))
        return row * GRID_COLS + col + 1

    def _setup_mqtt(self):
        """MQTT 클라이언트 설정"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message

        try:
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            print(f"[AGV {self.rid}] MQTT connecting...")
        except Exception as e:
            print(f"[AGV {self.rid}] MQTT connection failed: {e}")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        print(f"[AGV {self.rid}] MQTT connected, rc={rc}")
        client.subscribe(TOPIC_LOWCMD)
        print(f"[AGV {self.rid}] Subscribed: {TOPIC_LOWCMD}")
        self.mqtt_connected = True

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            return

        # rid 필터링
        if "rid" in cmd and int(cmd["rid"]) != self.rid:
            return

        new_target = cmd.get("target_node")
        v = float(cmd.get("v", 0.0))

        if new_target is not None:
            new_target = int(new_target)
            if new_target != self.target_node and self.state != "TURNING" and self.state != "MOVING":
                self._set_new_target(new_target)

        self.speed = v

    def _set_new_target(self, new_target):
        """새 목표 Node 설정"""
        self.target_node = new_target
        self.target_pos = self.node_to_world(new_target)
        self.progress = 0.0

        current_x, current_y = self.get_position()
        self.move_start_pos = (current_x, current_y)

        # 이동 방향 계산
        dx = self.target_pos[0] - current_x
        dy = self.target_pos[1] - current_y

        if abs(dx) < 0.01 and abs(dy) < 0.01:
            # 이미 목표 위치에 있음
            self.state = "IDLE"
            return

        # 90도 단위로 목표 각도 결정
        if abs(dx) > abs(dy):
            # X 방향 이동
            self.target_angle = DIR_EAST if dx > 0 else DIR_WEST
        else:
            # Y 방향 이동
            self.target_angle = DIR_NORTH if dy > 0 else DIR_SOUTH

        self.state = "TURNING"
        print(f"[AGV {self.rid}] New target: node {new_target}, angle: {math.degrees(self.target_angle):.0f}°")

    def get_position(self):
        """현재 GPS 위치"""
        values = self.gps.getValues()
        return values[0], values[1]

    def get_bearing(self):
        """현재 방향 (라디안, -π ~ π)"""
        values = self.compass.getValues()
        # Compass는 북쪽(+Y)을 기준으로 함
        angle = math.atan2(values[0], values[1])
        return angle

    def normalize_angle(self, angle):
        """각도를 -π ~ π 범위로 정규화"""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def set_motors(self, left, right):
        """모터 속도 설정"""
        self.left_motor.setVelocity(left)
        self.right_motor.setVelocity(right)

    def stop(self):
        """정지"""
        self.set_motors(0.0, 0.0)

    def update(self):
        """상태 머신 업데이트"""
        if self.speed <= 0 and self.state not in ["TURNING", "MOVING"]:
            self.stop()
            return

        current_x, current_y = self.get_position()
        current_angle = self.get_bearing()

        if self.state == "IDLE":
            self.stop()

        elif self.state == "TURNING":
            # 목표 각도로 회전
            angle_diff = self.normalize_angle(self.target_angle - current_angle)

            if abs(angle_diff) < ANGLE_TOLERANCE:
                # 회전 완료 → 이동 Started
                self.stop()
                self.state = "MOVING"
                self.move_start_pos = (current_x, current_y)
                print(f"[AGV {self.rid}] Turn complete, moving forward")
            else:
                # 제자리 회전 (디퍼렌셜 드라이브)
                turn_speed = TURN_SPEED if angle_diff > 0 else -TURN_SPEED
                # 급격한 회전 방지
                turn_speed = turn_speed * min(1.0, abs(angle_diff) / 0.5)
                self.set_motors(-turn_speed, turn_speed)

        elif self.state == "MOVING":
            # 직진 이동
            target_x, target_y = self.target_pos
            dx = target_x - current_x
            dy = target_y - current_y
            distance = math.sqrt(dx*dx + dy*dy)

            # 진행률 계산
            start_x, start_y = self.move_start_pos
            total_dist = math.sqrt((target_x - start_x)**2 + (target_y - start_y)**2)
            if total_dist > 0.01:
                self.progress = max(0.0, min(1.0, 1.0 - distance / total_dist))
            else:
                self.progress = 1.0

            if distance < POSITION_TOLERANCE:
                # arrived
                self.stop()
                self.current_node = self.target_node
                self.progress = 1.0
                self.state = "IDLE"
                print(f"[AGV {self.rid}] Node {self.current_node} arrived")
            else:
                # 직진 중 방향 보정
                angle_to_target = math.atan2(dy, dx)
                angle_diff = self.normalize_angle(angle_to_target - current_angle)

                # 직진하면서 약간의 방향 보정
                correction = angle_diff * 2.0
                correction = max(-1.0, min(1.0, correction))

                left_speed = MOVE_SPEED - correction
                right_speed = MOVE_SPEED + correction

                self.set_motors(left_speed, right_speed)

    def publish_state(self):
        """MQTT로 상태 발행"""
        if not self.mqtt_connected:
            return

        state = {
            "rid": self.rid,
            "current_node": self.current_node,
            "target_node": self.target_node,
            "progress": round(self.progress, 2),
            "state": self.state,
            "ts": int(time.time())
        }

        try:
            self.mqtt_client.publish(TOPIC_STATE, json.dumps(state), qos=0)
        except Exception as e:
            print(f"[AGV {self.rid}] State publish failed: {e}")

    def run(self):
        """메인 루프"""
        print(f"[AGV {self.rid}] Started")

        # 초기 센서 읽기 대기
        for _ in range(10):
            self.robot.step(self.timestep)

        state_counter = 0

        while self.robot.step(self.timestep) != -1:
            self.update()

            # 상태 발행 (약 10Hz)
            state_counter += 1
            if state_counter >= 6:
                self.publish_state()
                state_counter = 0

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


if __name__ == "__main__":
    controller = AGVController()
    controller.run()
