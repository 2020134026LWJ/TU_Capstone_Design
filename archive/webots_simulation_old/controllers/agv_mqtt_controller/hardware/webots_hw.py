"""Webots 하드웨어 구현"""

import math
import sys
from typing import Optional, Tuple

from controller import Supervisor

from .base import (
    CameraInterface,
    PositionInterface,
    MotorInterface,
    LiftInterface,
    ShelfSupervisorInterface,
    CollisionSensorInterface,
    TimestepInterface,
    HardwarePack,
)

try:
    import numpy as np
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

MARKER_DETECT_INTERVAL = 5


class WebotsCamera(CameraInterface):
    """Webots 하향 카메라 (BGRA → 그레이스케일)"""

    def __init__(self, robot: Supervisor, timestep: int):
        self._camera = None
        self._width = 0
        self._height = 0
        if CV2_AVAILABLE:
            self._camera = robot.getDevice("floor_camera")
            if self._camera:
                self._camera.enable(timestep * MARKER_DETECT_INTERVAL)
                self._width = self._camera.getWidth()
                self._height = self._camera.getHeight()

    def capture(self) -> Optional["np.ndarray"]:
        if not self._camera or not CV2_AVAILABLE:
            return None
        image = self._camera.getImage()
        if not image:
            return None
        img = np.frombuffer(image, np.uint8).reshape((self._height, self._width, 4))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

    def get_resolution(self) -> Tuple[int, int]:
        return self._width, self._height


class WebotsPosition(PositionInterface):
    """Webots GPS + Compass"""

    def __init__(self, robot: Supervisor, timestep: int):
        self._gps = robot.getDevice("gps")
        self._gps.enable(timestep)
        self._compass = robot.getDevice("compass")
        self._compass.enable(timestep)

    def get_position(self) -> Tuple[float, float]:
        values = self._gps.getValues()
        return values[0], values[1]

    def get_bearing(self) -> float:
        values = self._compass.getValues()
        return math.atan2(values[0], values[1])


class WebotsMotors(MotorInterface):
    """Webots 디퍼렌셜 드라이브 모터"""

    def __init__(self, robot: Supervisor):
        self._left = robot.getDevice("left wheel")
        self._right = robot.getDevice("right wheel")
        self._left.setPosition(float("inf"))
        self._right.setPosition(float("inf"))
        self._left.setVelocity(0.0)
        self._right.setVelocity(0.0)

    def set_speeds(self, left: float, right: float):
        self._left.setVelocity(left)
        self._right.setVelocity(right)

    def stop(self):
        self.set_speeds(0.0, 0.0)


class WebotsLift(LiftInterface):
    """Webots 리프트 모터"""

    def __init__(self, robot: Supervisor, timestep: int):
        self._motor = robot.getDevice("lift_motor")
        self._sensor = robot.getDevice("lift_sensor")
        self._sensor.enable(timestep)

    def lift_up(self):
        self._motor.setPosition(0.10)

    def lift_down(self):
        self._motor.setPosition(0.0)


class WebotsShelfSupervisor(ShelfSupervisorInterface):
    """Webots Supervisor API로 선반 이동"""

    def __init__(self, robot: Supervisor):
        self._robot = robot

    def move_shelf_to_robot(self, shelf_id: int, x: float, y: float):
        shelf_node = self._robot.getFromDef(f"SHELF_{shelf_id}")
        if shelf_node:
            shelf_node.getField("translation").setSFVec3f([x, y, 0.12])

    def detach_shelf(self, shelf_id: int, x: float, y: float):
        shelf_node = self._robot.getFromDef(f"SHELF_{shelf_id}")
        if shelf_node:
            shelf_node.getField("translation").setSFVec3f([x, y, 0])


class WebotsCollisionSensor(CollisionSensorInterface):
    """Webots Supervisor API로 다른 로봇 추적"""

    def __init__(self, robot: Supervisor, rid: int, other_rid: int):
        self._other_node = None
        other_def = f"AGV_{other_rid}"
        self._other_node = robot.getFromDef(other_def)
        if self._other_node:
            print(f"[AGV {rid}] Collision avoidance: tracking {other_def}")
        else:
            print(f"[AGV {rid}] Warning: {other_def} not found (no collision avoidance)")

    def get_other_robot_position(self) -> Optional[Tuple[float, float]]:
        if not self._other_node:
            return None
        try:
            pos = self._other_node.getField("translation").getSFVec3f()
            return pos[0], pos[1]
        except Exception:
            return None

    def get_other_robot_distance_and_ahead(
        self, x: float, y: float, heading: float
    ) -> Tuple[float, bool]:
        if not self._other_node:
            return float("inf"), False
        try:
            other_pos = self._other_node.getField("translation").getSFVec3f()
            dx = other_pos[0] - x
            dy = other_pos[1] - y
            dist = math.sqrt(dx * dx + dy * dy)

            angle_to_other = math.atan2(dy, dx)
            angle_diff = angle_to_other - heading
            while angle_diff > math.pi:
                angle_diff -= 2 * math.pi
            while angle_diff < -math.pi:
                angle_diff += 2 * math.pi
            is_ahead = abs(angle_diff) < math.pi / 2

            return dist, is_ahead
        except Exception:
            return float("inf"), False


class WebotsTimestep(TimestepInterface):
    """Webots robot.step()"""

    def __init__(self, robot: Supervisor, timestep: int):
        self._robot = robot
        self._timestep = timestep

    def step(self) -> bool:
        return self._robot.step(self._timestep) != -1

    def get_timestep_ms(self) -> int:
        return self._timestep


def create_webots_hardware() -> HardwarePack:
    """Webots 하드웨어 초기화 및 HardwarePack 반환"""
    robot = Supervisor()
    timestep = int(robot.getBasicTimeStep())

    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    start_node = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    other_rid = 2 if rid == 1 else 1

    print(f"[AGV {rid}] Init - start node: {start_node}")

    camera = WebotsCamera(robot, timestep)
    if camera._camera:
        print(f"[AGV {rid}] Camera + ArUco detector initialized")

    return HardwarePack(
        camera=camera,
        position=WebotsPosition(robot, timestep),
        motors=WebotsMotors(robot),
        lift=WebotsLift(robot, timestep),
        shelf_supervisor=WebotsShelfSupervisor(robot),
        collision=WebotsCollisionSensor(robot, rid, other_rid),
        timestep=WebotsTimestep(robot, timestep),
        rid=rid,
        start_node=start_node,
        other_rid=other_rid,
    )
