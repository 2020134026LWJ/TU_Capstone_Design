"""
설정 관리 모듈 — 서버 전역 설정값의 단일 출처(single source).

여기 모인 값들:
  - MQTT 호스트/포트 + 토픽 이름 (AGV ↔ 서버 통신 채널)
  - 맵/로봇/선반 설정 JSON 파일 경로 (data/ 기준)
  - A* 경로 계획 파라미터 (시간 horizon, goal 점유, 속도)

설계 의도:
  - dataclass라 필드만 보면 전체 설정 한눈에 파악 가능
  - 경로는 __post_init__에서 base_dir(=이 파일 위치) 기준으로 자동 완성 → 어디서 실행해도 동일
  - 운영값(host/port)은 from_env로 환경변수 오버라이드 가능 (코드 수정 없이 배치 변경)
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """서버 설정 — 모든 필드는 기본값을 가지므로 Config() 만으로 즉시 사용 가능."""

    # 수정 69 (A 배관) — 실물 카메라가 보고한 heading을 **제어에 쓸지**의 밸브.
    #
    # False(기본): 보고만 받고 장부와 비교해 로그만 찍는다. robot.heading은 안 건드린다.
    # True: 카메라 heading을 그대로 믿는다 → 서버가 방향을 추측(dead reckoning)하지 않게 된다.
    #
    # ⚠️**hardware/config.py의 HEADING_OFFSET을 실물에서 실측한 뒤에만 켤 것.**
    # 상수가 틀린 채로 켜면 서버가 매 마커마다 방향을 잘못 갱신해 지금보다 나빠진다.
    # 켜기 전 절차: 실물 주행 → 서버 로그의 `[heading] ... 차이 N°` 가 일정한지 본다 →
    #   그 N만큼 HEADING_OFFSET 보정 → 경고가 안 뜨면 맞은 것 → 그때 True.
    TRUST_CAMERA_HEADING: bool = False

    # ─── MQTT 설정 (AGV ↔ 서버, GUI ↔ 서버 메시징) ───
    mqtt_host: str = "localhost"              # 브로커 주소. 라파/노트북 분리 배치 시 from_env로 변경
    mqtt_port: int = 1883                     # mosquitto 기본 포트
    mqtt_topic_cmd: str = "/agv/cmd"          # 서버 → AGV 개별 명령 (forward/turn/lift)
    mqtt_topic_marker: str = "/agv/marker"    # AGV → 서버 마커 인식 보고 (위치 갱신 트리거)
    mqtt_topic_cmd_ack: str = "/agv/cmd_ack"  # AGV → 서버 명령 완료 보고 (turn/lift 완료)
    mqtt_topic_state: str = "/agv/state"      # (예비) AGV 상태 브로드캐스트용 — 현재 미사용

    # ─── 파일 경로 (빈 문자열이면 __post_init__이 data/ 기준으로 자동 채움) ───
    base_dir: str = ""                        # 이 파일이 있는 server/ 디렉토리 (런타임 결정)
    map_file: str = ""                        # 8×6 그리드 노드/엣지 정의 (map.json)
    robot_config_file: str = ""               # AGV 대수/홈 노드/초기 heading (robot_config.json)
    shelf_config_file: str = ""               # 선반 노드/작업대/회랑 게이트 (shelf_config.json)

    # ─── 경로 계획 파라미터 (PARAM: 튜닝 대상) ───
    max_time: int = 50              # PARAM: A* 시간 horizon (step 수). 늘리면 더 먼 경로/대기 가능
    stay_time_at_goal: int = 3      # PARAM: 도착 후 goal 노드 점유 유지 step 수 (충돌 회피용)
    default_speed: float = 0.3      # PARAM: AGV 기본 속도 (시뮬레이션 보간용, m/s 또는 unit/s)

    def __post_init__(self):
        """dataclass 생성 직후 호출 — 비어있는 경로 필드를 base_dir 기준으로 완성.

        base_dir를 이 파일(config.py) 위치로 잡으므로, 작업 디렉토리(cwd)가 어디든
        항상 server/data/ 의 JSON을 정확히 가리킨다. (상대경로 깨짐 방지)
        """
        if not self.base_dir:
            # __file__ = server/config.py → dirname = server/
            self.base_dir = os.path.dirname(os.path.abspath(__file__))
        if not self.map_file:
            self.map_file = os.path.join(self.base_dir, "data", "map.json")
        if not self.robot_config_file:
            self.robot_config_file = os.path.join(self.base_dir, "data", "robot_config.json")
        if not self.shelf_config_file:
            self.shelf_config_file = os.path.join(self.base_dir, "data", "shelf_config.json")

    @classmethod
    def from_env(cls) -> "Config":
        """환경 변수에서 운영값(host/port)을 읽어 Config 생성.

        코드 수정 없이 배치만 바꾸기 위함 (예: 라파 GUI ↔ 노트북 서버 핫스팟).
        지정 안 된 환경변수는 기본값 사용. 파일 경로는 __post_init__이 자동 처리.
        """
        return cls(
            mqtt_host=os.getenv("MQTT_HOST", "localhost"),
            mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        )


# 기본 설정 인스턴스 — 환경변수 불필요한 곳에서 즉시 import해 사용
default_config = Config()
