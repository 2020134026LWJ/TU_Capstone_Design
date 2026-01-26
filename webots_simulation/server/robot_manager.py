"""
로봇 관리 모듈
- 로봇 현재 위치 추적
- 작업 큐 관리
- 로봇 상태 (idle, busy, error)
"""

import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from .config import Config


class RobotStatus(Enum):
    """로봇 상태"""
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class Robot:
    """로봇 정보"""
    rid: int
    name: str
    home_node: int
    current_node: int
    status: RobotStatus = RobotStatus.IDLE
    current_task: Optional[Dict[str, Any]] = None
    task_queue: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rid": self.rid,
            "name": self.name,
            "home_node": self.home_node,
            "current_node": self.current_node,
            "status": self.status.value,
            "current_task": self.current_task,
            "queue_length": len(self.task_queue)
        }


class RobotManager:
    """로봇 관리자"""

    def __init__(self, config: Config):
        self.config = config
        self.robots: Dict[int, Robot] = {}
        self._load_robot_config()

    def _load_robot_config(self) -> None:
        """robot_config.json 로드"""
        try:
            with open(self.config.robot_config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            for rid_str, robot_info in data.get("robots", {}).items():
                rid = int(rid_str)
                self.robots[rid] = Robot(
                    rid=rid,
                    name=robot_info.get("name", f"AGV-{rid}"),
                    home_node=robot_info.get("home_node", rid),
                    current_node=robot_info.get("home_node", rid)
                )

            print(f"[RobotManager] Loaded {len(self.robots)} robots from {self.config.robot_config_file}")

        except FileNotFoundError:
            print(f"[RobotManager] Config not found, using defaults")
            # 기본 2대 로봇 설정
            self.robots[1] = Robot(rid=1, name="AGV-1", home_node=1, current_node=1)
            self.robots[2] = Robot(rid=2, name="AGV-2", home_node=37, current_node=37)

        except Exception as e:
            print(f"[RobotManager] Error loading config: {e}")
            # 기본 설정
            self.robots[1] = Robot(rid=1, name="AGV-1", home_node=1, current_node=1)
            self.robots[2] = Robot(rid=2, name="AGV-2", home_node=37, current_node=37)

    def get_robot(self, rid: int) -> Optional[Robot]:
        """로봇 조회"""
        return self.robots.get(rid)

    def get_all_robots(self) -> List[Robot]:
        """모든 로봇 조회"""
        return list(self.robots.values())

    def get_idle_robot(self) -> Optional[Robot]:
        """유휴 로봇 조회"""
        for robot in self.robots.values():
            if robot.status == RobotStatus.IDLE:
                return robot
        return None

    def update_robot_position(self, rid: int, node: int) -> bool:
        """로봇 위치 업데이트"""
        robot = self.robots.get(rid)
        if robot:
            robot.current_node = node
            return True
        return False

    def set_robot_status(self, rid: int, status: RobotStatus) -> bool:
        """로봇 상태 변경"""
        robot = self.robots.get(rid)
        if robot:
            robot.status = status
            return True
        return False

    def assign_task(self, rid: int, task: Dict[str, Any]) -> bool:
        """로봇에 작업 할당"""
        robot = self.robots.get(rid)
        if not robot:
            return False

        if robot.status == RobotStatus.IDLE:
            robot.current_task = task
            robot.status = RobotStatus.BUSY
            print(f"[RobotManager] Robot {rid}: assigned task {task}")
            return True
        else:
            # 작업 큐에 추가
            robot.task_queue.append(task)
            print(f"[RobotManager] Robot {rid}: task queued (queue size: {len(robot.task_queue)})")
            return True

    def complete_task(self, rid: int) -> Optional[Dict[str, Any]]:
        """작업 완료 처리"""
        robot = self.robots.get(rid)
        if not robot:
            return None

        completed_task = robot.current_task
        robot.current_task = None

        # 대기 중인 작업이 있으면 다음 작업 시작
        if robot.task_queue:
            robot.current_task = robot.task_queue.pop(0)
            print(f"[RobotManager] Robot {rid}: starting next task from queue")
        else:
            robot.status = RobotStatus.IDLE
            print(f"[RobotManager] Robot {rid}: now idle")

        return completed_task

    def get_robot_by_worker(self, worker_id: int) -> Optional[Robot]:
        """작업자 ID로 로봇 조회 (worker_id = robot_id로 매핑)"""
        return self.robots.get(worker_id)

    def get_status_summary(self) -> Dict[str, Any]:
        """전체 상태 요약"""
        return {
            "total_robots": len(self.robots),
            "idle": sum(1 for r in self.robots.values() if r.status == RobotStatus.IDLE),
            "busy": sum(1 for r in self.robots.values() if r.status == RobotStatus.BUSY),
            "error": sum(1 for r in self.robots.values() if r.status == RobotStatus.ERROR),
            "robots": [r.to_dict() for r in self.robots.values()]
        }
