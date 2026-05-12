"""
로봇 관리 모듈
- 로봇 현재 위치 추적
- 확장된 상태 머신 (idle, moving_to_shelf, picking_up, delivering, waiting, returning)
- 선반 운반 상태 관리
"""

import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from .config import Config


class RobotStatus(Enum):
    """로봇 상태"""
    IDLE = "idle"
    MOVING_TO_SHELF = "moving_to_shelf"         # 선반으로 이동 중
    PICKING_UP_SHELF = "picking_up_shelf"        # 선반 들어올리는 중
    DELIVERING_TO_WS = "delivering_to_ws"        # 작업대로 배달 중
    WAITING_FOR_PICK = "waiting_for_pick"        # 작업자 픽업 대기
    RETURNING_SHELF = "returning_shelf"          # 선반 복귀 중
    ERROR = "error"


@dataclass
class Robot:
    """로봇 정보"""
    rid: int
    name: str
    home_node: int
    current_node: int
    status: RobotStatus = RobotStatus.IDLE
    carrying_shelf: Optional[int] = None            # 운반 중인 선반 ID
    current_task_id: Optional[str] = None           # 현재 작업 ID
    current_task: Optional[Dict[str, Any]] = None
    task_queue: List[Dict[str, Any]] = field(default_factory=list)

    # 명령 기반 이동
    heading: int = 0                                 # 현재 방향 (0=북, 90=동, 180=남, 270=서)
    heading_initialized: bool = False                # 첫 마커 보고로 heading 확인됐는지 여부
    command_queue: List[str] = field(default_factory=list)  # 전송 대기 명령 리스트
    planned_path: List[int] = field(default_factory=list)   # 현재 계획된 노드 경로

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rid": self.rid,
            "name": self.name,
            "home_node": self.home_node,
            "current_node": self.current_node,
            "status": self.status.value,
            "carrying_shelf": self.carrying_shelf,
            "current_task_id": self.current_task_id,
            "current_task": self.current_task,
            "queue_length": len(self.task_queue),
            "heading": self.heading,
        }


class RobotManager:
    """로봇 관리자"""

    # ─── 초기화 ───

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
                    current_node=robot_info.get("home_node", rid),
                )

            print(f"[RobotManager] Loaded {len(self.robots)} robots from {self.config.robot_config_file}")

        except FileNotFoundError:
            print(f"[RobotManager] Config not found, using defaults")
            self.robots[1] = Robot(rid=1, name="AGV-1", home_node=33, current_node=33)
            self.robots[2] = Robot(rid=2, name="AGV-2", home_node=34, current_node=34)

        except Exception as e:
            print(f"[RobotManager] Error loading config: {e}")
            self.robots[1] = Robot(rid=1, name="AGV-1", home_node=33, current_node=33)
            self.robots[2] = Robot(rid=2, name="AGV-2", home_node=34, current_node=34)

    # ─── 조회 ───

    def get_robot(self, rid: int) -> Optional[Robot]:
        """로봇 조회"""
        return self.robots.get(rid)

    def get_all_robots(self) -> List[Robot]:
        """모든 로봇 조회"""
        return list(self.robots.values())

    def get_available_robot(
        self,
        target_node: int = None,
        path_planner=None,
        dedicated_rid: int = None,  # [DEMO MODE] 지정 시 해당 로봇만 반환 (idle이면)
    ) -> Optional[Robot]:
        """유휴 로봇 조회 (target_node 지정 시 가장 가까운 로봇 우선)"""
        # [DEMO MODE] 특정 로봇 전담 배정
        if dedicated_rid is not None:
            robot = self.robots.get(dedicated_rid)
            if robot and robot.status == RobotStatus.IDLE and robot.heading_initialized:
                return robot
            return None  # 전담 로봇이 유휴가 아니면 대기

        idle_robots = [r for r in self.robots.values()
                       if r.status == RobotStatus.IDLE and r.heading_initialized]
        if not idle_robots:
            return None

        if target_node is not None and path_planner is not None:
            idle_robots.sort(key=lambda r: path_planner._heuristic(r.current_node, target_node))

        return idle_robots[0]

    # ─── 업데이트 ───

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
            old_status = robot.status
            robot.status = status
            print(f"[RobotManager] Robot {rid}: {old_status.value} -> {status.value}")
            return True
        return False

    def apply_turn(self, rid: int, cmd: str) -> bool:
        """turn 명령 완료 시 heading 갱신 (cmd_ack 처리용)

        Args:
            rid: 로봇 ID
            cmd: "turn_left" / "turn_right" / "turn_180"

        Returns:
            True 갱신 성공, False 로봇 없음 또는 잘못된 cmd
        """
        robot = self.robots.get(rid)
        if not robot:
            return False
        if cmd == "turn_right":
            robot.heading = (robot.heading + 90) % 360
        elif cmd == "turn_left":
            robot.heading = (robot.heading + 270) % 360
        elif cmd == "turn_180":
            robot.heading = (robot.heading + 180) % 360
        else:
            return False
        print(f"[RobotManager] Robot {rid}: heading updated to {robot.heading}° after {cmd}")
        return True

    def set_carrying_shelf(self, rid: int, shelf_id: Optional[int]) -> bool:
        """로봇 선반 운반 상태 설정"""
        robot = self.robots.get(rid)
        if robot:
            robot.carrying_shelf = shelf_id
            return True
        return False

    def get_robot_carrying_shelf(self, shelf_id: int) -> Optional[Robot]:
        """특정 선반을 운반 중인 로봇 찾기"""
        for robot in self.robots.values():
            if robot.carrying_shelf == shelf_id:
                return robot
        return None

    def assign_task(self, rid: int, task: Dict[str, Any]) -> bool:
        """로봇에 작업 할당"""
        robot = self.robots.get(rid)
        if not robot:
            return False

        if robot.status == RobotStatus.IDLE:
            robot.current_task = task
            robot.current_task_id = task.get("task_id")
            robot.status = RobotStatus.MOVING_TO_SHELF
            print(f"[RobotManager] Robot {rid}: assigned task {task.get('task_id', 'unknown')}")
            return True
        else:
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
        robot.current_task_id = None
        robot.carrying_shelf = None

        if robot.task_queue:
            robot.current_task = robot.task_queue.pop(0)
            robot.current_task_id = robot.current_task.get("task_id")
            robot.status = RobotStatus.MOVING_TO_SHELF
            print(f"[RobotManager] Robot {rid}: starting next task from queue")
        else:
            robot.status = RobotStatus.IDLE
            print(f"[RobotManager] Robot {rid}: now idle")

        return completed_task

    # ─── 상태 ───

    def get_status_summary(self) -> Dict[str, Any]:
        """전체 상태 요약"""
        status_counts = {}
        for r in self.robots.values():
            status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1

        return {
            "total_robots": len(self.robots),
            "status_counts": status_counts,
            "robots": [r.to_dict() for r in self.robots.values()],
        }
