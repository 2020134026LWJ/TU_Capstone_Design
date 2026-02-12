"""하드웨어 추상 인터페이스 (ABC)"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    import numpy as np
except ImportError:
    np = None


class CameraInterface(ABC):
    """카메라 인터페이스"""

    @abstractmethod
    def capture(self) -> Optional["np.ndarray"]:
        """그레이스케일 이미지 캡처"""
        ...

    @abstractmethod
    def get_resolution(self) -> Tuple[int, int]:
        """(width, height) 반환"""
        ...


class PositionInterface(ABC):
    """위치 센서 인터페이스"""

    @abstractmethod
    def get_position(self) -> Tuple[float, float]:
        """(x, y) 좌표 반환"""
        ...

    @abstractmethod
    def get_bearing(self) -> float:
        """방향각 (라디안) 반환"""
        ...

    def update(self) -> None:
        """매 타임스텝 센서 데이터 갱신 (오버라이드 가능)"""
        pass


class MotorInterface(ABC):
    """모터 인터페이스"""

    @abstractmethod
    def set_speeds(self, left: float, right: float):
        """좌우 모터 속도 설정"""
        ...

    @abstractmethod
    def stop(self):
        """모터 정지"""
        ...


class LiftInterface(ABC):
    """리프트 인터페이스"""

    @abstractmethod
    def lift_up(self):
        """리프트 올림"""
        ...

    @abstractmethod
    def lift_down(self):
        """리프트 내림"""
        ...


class ShelfSupervisorInterface(ABC):
    """선반 Supervisor 인터페이스"""

    @abstractmethod
    def move_shelf_to_robot(self, shelf_id: int, x: float, y: float):
        """선반을 로봇 위치로 이동"""
        ...

    @abstractmethod
    def detach_shelf(self, shelf_id: int, x: float, y: float):
        """선반을 현재 위치에 내려놓기"""
        ...


class CollisionSensorInterface(ABC):
    """충돌 감지 인터페이스"""

    @abstractmethod
    def get_other_robot_distance_and_ahead(
        self, x: float, y: float, heading: float
    ) -> Tuple[float, bool]:
        """다른 로봇의 거리 및 전방 여부 반환"""
        ...


class TimestepInterface(ABC):
    """시뮬레이션/실행 타임스텝 인터페이스"""

    @abstractmethod
    def step(self) -> bool:
        """한 타임스텝 진행. True=계속, False=종료"""
        ...

    @abstractmethod
    def get_timestep_ms(self) -> int:
        """타임스텝 (밀리초)"""
        ...


@dataclass
class HardwarePack:
    """하드웨어 인터페이스 모음"""
    camera: Optional[CameraInterface]
    position: PositionInterface
    motors: MotorInterface
    lift: LiftInterface
    shelf_supervisor: ShelfSupervisorInterface
    collision: CollisionSensorInterface
    timestep: TimestepInterface
    rid: int
    start_node: int
    other_rid: int
