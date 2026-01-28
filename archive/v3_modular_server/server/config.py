"""
설정 관리 모듈
- MQTT 호스트/포트
- WebSocket 포트
- 맵 파일 경로
- 로봇 설정 파일 경로
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """서버 설정"""
    # MQTT 설정
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic_plan: str = "/agv/plan"
    mqtt_topic_state: str = "/agv/state"

    # WebSocket 설정
    websocket_host: str = "0.0.0.0"
    websocket_port: int = 8765

    # 파일 경로
    base_dir: str = ""
    map_file: str = ""
    robot_config_file: str = ""

    # 경로 계획 설정
    max_time: int = 50
    stay_time_at_goal: int = 3
    default_speed: float = 0.3

    def __post_init__(self):
        if not self.base_dir:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not self.map_file:
            self.map_file = os.path.join(self.base_dir, "map.json")
        if not self.robot_config_file:
            self.robot_config_file = os.path.join(self.base_dir, "robot_config.json")

    @classmethod
    def from_env(cls) -> "Config":
        """환경 변수에서 설정 로드"""
        return cls(
            mqtt_host=os.getenv("MQTT_HOST", "localhost"),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
            websocket_host=os.getenv("WS_HOST", "0.0.0.0"),
            websocket_port=int(os.getenv("WS_PORT", "8765")),
        )


# 기본 설정 인스턴스
default_config = Config()
