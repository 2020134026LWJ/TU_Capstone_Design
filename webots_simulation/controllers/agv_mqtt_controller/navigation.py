"""공통 내비게이션 모듈 - 상태머신 + 경로 추종 (실물/시뮬 동일)"""

import math
from typing import Callable, List, Optional, Tuple

from hardware.base import CollisionSensorInterface, MotorInterface, PositionInterface

# ─── 그리드 설정 ───
CELL_SIZE = 1.0
GRID_COLS = 8
GRID_ROWS = 4

# ─── 주행 파라미터 ───
MOVE_SPEED = 3.0
TURN_SPEED = 2.0
POSITION_TOLERANCE = 0.05
ANGLE_TOLERANCE = 0.03

# ─── 충돌 회피 ───
COLLISION_SLOW_DIST = 0.7
COLLISION_STOP_DIST = 0.45

# ─── 방향 ───
DIR_EAST = 0.0
DIR_NORTH = math.pi / 2
DIR_WEST = math.pi
DIR_SOUTH = -math.pi / 2


class Navigator:
    """경로 추종 + 상태머신 + 충돌 회피"""

    def __init__(
        self,
        position: PositionInterface,
        motors: MotorInterface,
        collision: CollisionSensorInterface,
        rid: int,
        other_rid: int,
        start_node: int,
    ):
        self._position = position
        self._motors = motors
        self._collision = collision
        self._rid = rid
        self._other_rid = other_rid

        # ─── 상태 ───
        self.current_node = start_node
        self.state = "IDLE"
        self.target_angle = 0.0
        self.target_pos = self.node_to_world(start_node)
        self.move_start_pos = self.target_pos
        self._moving_to_node = start_node

        # ─── 경로 추종 ───
        self.path_queue: List[int] = []
        self.goal_node: Optional[int] = None

        # ─── 콜백 ───
        self._on_arrived_callback: Optional[Callable[[int], None]] = None

    def set_on_arrived(self, callback: Callable[[int], None]):
        """목표 도착 시 콜백 설정"""
        self._on_arrived_callback = callback

    # ─── 좌표 변환 ───

    @staticmethod
    def node_to_world(node_id: int) -> Tuple[float, float]:
        """Node ID → 월드 좌표 (4x8 grid + workstation 33,34)"""
        if node_id == 33:
            return -0.5, 0.5
        elif node_id == 34:
            return -0.5, 3.5
        idx = node_id - 1
        col = idx % GRID_COLS
        row = idx // GRID_COLS
        return (col + 0.5) * CELL_SIZE, (row + 0.5) * CELL_SIZE

    @staticmethod
    def normalize_angle(angle: float) -> float:
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    # ─── 경로 설정 ───

    def set_plan(self, path_queue: List[int], goal: int):
        """경로 추종 시작"""
        self.path_queue = path_queue
        self.goal_node = goal
        self._move_to_next_node()

    def set_at_goal(self, goal: int):
        """이미 목표에 위치한 경우"""
        self.goal_node = goal
        self.current_node = goal

    # ─── 충돌 회피 ───

    def get_collision_speed_factor(self) -> float:
        """방향 인식 충돌 회피 - 다른 로봇이 전방에 있을 때만 감속"""
        x, y = self._position.get_position()
        heading = self._position.get_bearing()
        dist, is_ahead = self._collision.get_other_robot_distance_and_ahead(
            x, y, heading
        )

        if not is_ahead:
            return 1.0

        if dist <= COLLISION_STOP_DIST:
            if self._rid < self._other_rid:
                return 0.2
            return 0.0
        elif dist <= COLLISION_SLOW_DIST:
            factor = (dist - COLLISION_STOP_DIST) / (
                COLLISION_SLOW_DIST - COLLISION_STOP_DIST
            )
            if self._rid < self._other_rid:
                return max(0.3, factor)
            return factor
        return 1.0

    # ─── 경로 추종 (내부) ───

    def _move_to_next_node(self):
        if not self.path_queue:
            return
        next_node = self.path_queue.pop(0)
        self._set_target(next_node)

    def _set_target(self, node: int):
        self._moving_to_node = node
        self.target_pos = self.node_to_world(node)

        cx, cy = self._position.get_position()
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

    def _on_node_reached(self, node: int):
        self.current_node = node
        print(f"[AGV {self._rid}] Reached node {node}")

        if self.path_queue:
            self._move_to_next_node()
        else:
            self.state = "IDLE"
            self._motors.stop()
            if self.goal_node is not None:
                if self._on_arrived_callback:
                    self._on_arrived_callback(node)
                self.goal_node = None

    # ─── 상태 머신 ───

    def update(self):
        speed_factor = self.get_collision_speed_factor()

        if self.state == "IDLE":
            self._motors.stop()

        elif self.state == "TURNING":
            if speed_factor <= 0.0:
                self._motors.stop()
                return

            current_angle = self._position.get_bearing()
            angle_diff = self.normalize_angle(self.target_angle - current_angle)

            if abs(angle_diff) < ANGLE_TOLERANCE:
                self._motors.stop()
                self.state = "MOVING"
                cx, cy = self._position.get_position()
                self.move_start_pos = (cx, cy)
            else:
                speed = TURN_SPEED if angle_diff > 0 else -TURN_SPEED
                speed *= min(1.0, abs(angle_diff) / 0.5) * speed_factor
                self._motors.set_speeds(-speed, speed)

        elif self.state == "MOVING":
            if speed_factor <= 0.0:
                self._motors.stop()
                return

            cx, cy = self._position.get_position()
            tx, ty = self.target_pos
            dx, dy = tx - cx, ty - cy
            distance = math.sqrt(dx * dx + dy * dy)

            if distance < POSITION_TOLERANCE:
                self._motors.stop()
                self._on_node_reached(self._moving_to_node)
            else:
                current_angle = self._position.get_bearing()
                angle_to_target = math.atan2(dy, dx)
                angle_diff = self.normalize_angle(angle_to_target - current_angle)
                correction = max(-1.0, min(1.0, angle_diff * 2.0))
                move_spd = MOVE_SPEED * speed_factor
                self._motors.set_speeds(move_spd - correction, move_spd + correction)
