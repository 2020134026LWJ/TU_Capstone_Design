"""
로봇 관리 모듈
- 로봇 현재 위치 추적
- 확장된 상태 머신 (idle, moving_to_shelf, picking_up, delivering, waiting, returning)
- 선반 운반 상태 관리
"""

import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

from ..config import Config


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

    # 존재 여부 (수정 75) — 두 플래그의 의미가 다르다. 합치지 말 것.
    #   online    = 지금 MQTT로 붙어 있나        → 태스크를 줘도 되나 (배정 게이트)
    #   ever_seen = 한 번이라도 붙은 적 있나     → 바닥에 실재하나 (A* 장애물 여부)
    # 주행 중 통신이 끊긴 로봇은 online=False 지만 ever_seen=True 다 — 몸은 그 칸에
    # 그대로 서 있으므로 **계속 피해 다녀야 한다**. 반대로 한 번도 안 켠 로봇은
    # 바닥에 없으므로 길을 막으면 안 된다.
    online: bool = False
    ever_seen: bool = False

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
            "online": self.online,
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
        # 수정 74: IDLE 전이 시 호출될 콜백 (core가 등록 → 예약 청소).
        # 호출부마다 청소를 붙이면 반드시 빠뜨린다 → 상태 전이 한 곳에서 발화시킨다.
        self._on_idle: Optional[Callable[[int], None]] = None
        self._load_robot_config()

    def on_idle(self, callback: Callable[[int], None]) -> None:
        """로봇이 IDLE로 전이할 때 호출될 콜백 등록. 인자: rid."""
        self._on_idle = callback

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

        except (FileNotFoundError, ValueError, KeyError) as e:
            # 폴백으로 홈 노드를 '추측'하지 않는다.
            # 예전엔 여기서 home 33/34를 만들어 넣었는데, 실제 홈은 9/33이라 이미 틀린 값이었다.
            # 설정을 못 읽으면 로봇이 엉뚱한 곳을 집으로 알고 도는 것보다 즉시 멈추는 게 안전하다.
            raise RuntimeError(
                f"[RobotManager] robot_config.json을 읽을 수 없음: "
                f"{self.config.robot_config_file} ({e}). 홈 노드는 추측하지 않는다."
            ) from e

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
        """유휴 로봇 조회 (target_node 지정 시 가장 가까운 로봇 우선).

        가용 = online + status IDLE + heading 초기화 + planned_path 비어있음 (= 진짜 정지).
        IDLE 상태로 parking 노드 이동 중인 로봇은 planned_path가 남아있음 → 제외.
        (수정 48: in-flight forward 중인 IDLE 로봇에 lift_up 명령 발행되어 엉뚱한
         노드에서 빈 lift 실행되는 race 차단)
        (수정 75: online — 안 켠 AGV에 태스크를 주면 서버 장부에만 존재하는 유령이
         일하게 된다. 전에는 heading_initialized가 우연히 이걸 막고 있었다.)
        """
        def _usable(r: Robot) -> bool:
            return (r.online and r.status == RobotStatus.IDLE
                    and r.heading_initialized and not r.planned_path)

        # [DEMO MODE] 특정 로봇 전담 배정
        if dedicated_rid is not None:
            robot = self.robots.get(dedicated_rid)
            if robot and _usable(robot):
                return robot
            return None  # 전담 로봇이 유휴가 아니면 대기

        idle_robots = [r for r in self.robots.values() if _usable(r)]
        if not idle_robots:
            return None

        if target_node is not None and path_planner is not None:
            idle_robots.sort(key=lambda r: path_planner._heuristic(r.current_node, target_node))

        return idle_robots[0]

    # ─── 업데이트 ───

    def set_presence(self, rid: int, online: bool) -> bool:
        """AGV 접속/이탈 반영 (수정 75). 상태가 실제로 바뀌었으면 True.

        online=True 는 두 곳에서 온다:
          1) /agv/presence 의 birth 메시지 (브릿지가 접속하며 발행)
          2) 그 AGV의 마커/ack 보고 — 말을 걸어왔으면 있는 것이다.
             (presence를 안 쏘는 구버전 클라이언트를 위한 fallback)
        online=False 는 브로커의 LWT 로만 온다 = 통신이 끊겼다.
        **몸은 그 자리에 남아있으므로 ever_seen 은 절대 되돌리지 않는다.**
        """
        robot = self.robots.get(rid)
        if not robot:
            return False
        changed = robot.online != online
        robot.online = online
        if online:
            robot.ever_seen = True
        return changed

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
            # 수정 74: 멈추는 순간 = 예약을 반납해야 하는 순간.
            # IDLE 로봇은 다시 계획하지 않으므로 아무도 그 예약을 대신 지워주지 않는다.
            if status == RobotStatus.IDLE and self._on_idle is not None:
                self._on_idle(rid)
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
            # 수정 74: 직접 대입 금지 — set_robot_status를 거쳐야 IDLE 콜백(예약 청소)이 발화한다
            print(f"[RobotManager] Robot {rid}: now idle")
            self.set_robot_status(rid, RobotStatus.IDLE)

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
