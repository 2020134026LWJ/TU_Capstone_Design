"""
pytest 픽스처 — 단위/통합 테스트 기반

핵심:
- MockMqttPublisher: publish_cmd / client.publish 호출 기록
- make_request_handler(): 모든 매니저를 실제 인스턴스로 묶은 RequestHandler
- step_marker / step_cmd_ack: AGV 메시지 시뮬 헬퍼
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

# 프로젝트 루트를 sys.path에 추가 → `from server.x import y` 가능
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.config import Config
from server.planning.path_planner import PathPlanner
from server.managers.robot import RobotManager
from server.managers.shelf import ShelfManager
from server.managers.staging import StagingManager
from server.managers.task import TaskManager
from server.core.request_handler import RequestHandler


# ─── Mock MQTT ────────────────────────────────────────────────

@dataclass
class _MockClient:
    """mqtt_client.client 대용 — broadcast 발행만 stub"""
    broadcasts: List[Tuple[str, Any]] = field(default_factory=list)

    def publish(self, topic: str, payload: Any, qos: int = 0):
        self.broadcasts.append((topic, payload))


@dataclass
class MockMqttPublisher:
    """publish_cmd 호출 + broadcast 기록. 실제 MQTT 연결 없음."""
    cmds: List[Tuple[int, str]] = field(default_factory=list)
    subscriptions: Dict[str, Callable] = field(default_factory=dict)
    client: _MockClient = field(default_factory=_MockClient)

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def subscribe(self, topic: str, callback: Callable) -> None:
        self.subscriptions[topic] = callback

    def publish_cmd(self, rid: int, cmd: str) -> bool:
        self.cmds.append((rid, cmd))
        return True

    # ─── 테스트 편의 메서드 ───
    def cmds_for(self, rid: int) -> List[str]:
        return [c for r, c in self.cmds if r == rid]

    def last_cmd(self, rid: int) -> Optional[str]:
        for r, c in reversed(self.cmds):
            if r == rid:
                return c
        return None

    def reset(self) -> None:
        self.cmds.clear()
        self.client.broadcasts.clear()


# ─── 픽스처 ────────────────────────────────────────────────

@pytest.fixture
def config() -> Config:
    """기본 Config — server/ 디렉토리의 map.json / robot_config.json / shelf_config.json 사용"""
    return Config()


@pytest.fixture
def mock_mqtt() -> MockMqttPublisher:
    return MockMqttPublisher()


@pytest.fixture
def handler(config: Config, mock_mqtt: MockMqttPublisher) -> RequestHandler:
    """
    실제 매니저 + Mock MQTT로 구성한 RequestHandler.

    main.py의 AGVServer.__init__을 그대로 따라가되 MQTTClient만 대체.
    """
    path_planner = PathPlanner(config.map_file)
    robot_manager = RobotManager(config)
    shelf_manager = ShelfManager(config.shelf_config_file)
    staging_manager = StagingManager(
        shelf_manager.workstations,
        get_robot_node=lambda rid: (
            robot_manager.get_robot(rid).current_node
            if robot_manager.get_robot(rid) else None
        ),
        get_robot_planned_path=lambda rid: (
            robot_manager.get_robot(rid).planned_path
            if robot_manager.get_robot(rid) else None
        ),
    )
    task_manager = TaskManager(shelf_manager, path_planner)

    rh = RequestHandler(
        config=config,
        path_planner=path_planner,
        mqtt_publisher=mock_mqtt,
        robot_manager=robot_manager,
        shelf_manager=shelf_manager,
        staging_manager=staging_manager,
        task_manager=task_manager,
    )
    return rh


# ─── 헬퍼 (테스트에서 import해서 사용) ────────────────────────────

def step_marker(handler: RequestHandler, rid: int, marker_id: int,
                heading: Optional[int] = None) -> Dict[str, Any]:
    """AGV → 서버 마커 인식 메시지 1개 주입"""
    payload: Dict[str, Any] = {"type": "marker_report", "rid": rid, "marker_id": marker_id}
    if heading is not None:
        payload["heading"] = heading
    return handler.handle_message(json.dumps(payload))


def step_cmd_ack(handler: RequestHandler, rid: int, cmd: str) -> Dict[str, Any]:
    """AGV → 서버 명령 완료 보고 1개 주입 (turn/lift)"""
    payload = {"type": "cmd_ack", "rid": rid, "cmd": cmd}
    return handler.handle_message(json.dumps(payload))


def init_robot_heading(handler: RequestHandler, rid: int, heading: int = 0) -> None:
    """heading_initialized=True 강제 설정 (테스트 시작 시 마커 1번 통과한 셈)"""
    robot = handler.robot_manager.get_robot(rid)
    assert robot is not None, f"AGV-{rid} not found"
    robot.heading = heading
    robot.heading_initialized = True


@pytest.fixture
def helpers():
    """테스트에서 헬퍼 함수에 접근하는 단일 진입점"""
    return type("Helpers", (), {
        "step_marker": staticmethod(step_marker),
        "step_cmd_ack": staticmethod(step_cmd_ack),
        "init_robot_heading": staticmethod(init_robot_heading),
    })
