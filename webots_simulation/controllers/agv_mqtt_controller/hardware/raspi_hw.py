"""Raspberry Pi 하드웨어 구현 (스텁 + 친구 코드 통합 준비)"""

import time
from typing import Optional, Tuple

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
except ImportError:
    np = None


class RaspiCamera(CameraInterface):
    """Picamera2 카메라 (TODO: 친구 코드에서 캘리브레이션 통합)"""

    def __init__(self):
        self._camera = None
        self._width = 640
        self._height = 480
        try:
            from picamera2 import Picamera2
            self._camera = Picamera2()
            self._camera.configure(
                self._camera.create_still_configuration(
                    main={"size": (self._width, self._height), "format": "RGB888"}
                )
            )
            self._camera.start()
            print("[RPi] Camera initialized")
        except Exception as e:
            print(f"[RPi] Camera not available: {e}")

    def capture(self) -> Optional["np.ndarray"]:
        if self._camera is None or np is None:
            return None
        try:
            import cv2
            frame = self._camera.capture_array()
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            # TODO: cv2.undistort(gray, camera_matrix, dist_coeffs) 적용
            return gray
        except Exception:
            return None

    def get_resolution(self) -> Tuple[int, int]:
        return self._width, self._height


class RaspiPositionSensor(PositionInterface):
    """ArUco 마커 기반 위치 추정 (TODO: 친구 코드에서 rvec/tvec 통합)"""

    def __init__(self):
        self._x = 0.0
        self._y = 0.0
        self._bearing = 0.0

    def update_from_marker(self, x: float, y: float, yaw: float):
        """ArUco 마커 인식 결과로 위치 업데이트"""
        self._x = x
        self._y = y
        self._bearing = yaw

    def get_position(self) -> Tuple[float, float]:
        return self._x, self._y

    def get_bearing(self) -> float:
        return self._bearing


class RaspiMotors(MotorInterface):
    """UART 시리얼 모터 제어 (TODO: 친구 코드에서 통합)"""

    def __init__(self):
        self._serial = None
        try:
            import serial
            self._serial = serial.Serial("/dev/ttyAMA0", 115200, timeout=0.1)
            print("[RPi] Motor serial initialized")
        except Exception as e:
            print(f"[RPi] Motor serial not available: {e}")

    def set_speeds(self, left: float, right: float):
        if self._serial:
            # TODO: 친구 코드의 UART 프로토콜에 맞게 구현
            cmd = f"M {left:.2f} {right:.2f}\n"
            try:
                self._serial.write(cmd.encode())
            except Exception:
                pass

    def stop(self):
        self.set_speeds(0.0, 0.0)


class RaspiLift(LiftInterface):
    """GPIO/UART 리프트 제어 (TODO: 스텁)"""

    def lift_up(self):
        print("[RPi] Lift UP (stub)")
        # TODO: GPIO 또는 UART 명령

    def lift_down(self):
        print("[RPi] Lift DOWN (stub)")
        # TODO: GPIO 또는 UART 명령


class RaspiShelfSupervisor(ShelfSupervisorInterface):
    """RPi에서는 선반 이동 불필요 (no-op)"""

    def move_shelf_to_robot(self, shelf_id: int, x: float, y: float):
        pass  # 실물에서는 물리적으로 리프트가 선반을 올림

    def detach_shelf(self, shelf_id: int, x: float, y: float):
        pass  # 실물에서는 물리적으로 리프트가 선반을 내림


class RaspiCollisionSensor(CollisionSensorInterface):
    """RPi에서는 서버가 충돌 관리 (no-op)"""

    def get_other_robot_distance_and_ahead(
        self, x: float, y: float, heading: float
    ) -> Tuple[float, bool]:
        return float("inf"), False


class RaspiTimestep(TimestepInterface):
    """실시간 타임스텝 (time.sleep)"""

    def __init__(self, timestep_ms: int = 32):
        self._timestep_ms = timestep_ms
        self._running = True

    def step(self) -> bool:
        time.sleep(self._timestep_ms / 1000.0)
        return self._running

    def get_timestep_ms(self) -> int:
        return self._timestep_ms

    def request_stop(self):
        self._running = False


def create_raspi_hardware(rid: int = 0, start_node: int = 1) -> HardwarePack:
    """RPi 하드웨어 초기화 및 HardwarePack 반환"""
    other_rid = 2 if rid == 1 else 1

    print(f"[AGV {rid}] RPi Init - start node: {start_node}")

    return HardwarePack(
        camera=RaspiCamera(),
        position=RaspiPositionSensor(),
        motors=RaspiMotors(),
        lift=RaspiLift(),
        shelf_supervisor=RaspiShelfSupervisor(),
        collision=RaspiCollisionSensor(),
        timestep=RaspiTimestep(),
        rid=rid,
        start_node=start_node,
        other_rid=other_rid,
    )
