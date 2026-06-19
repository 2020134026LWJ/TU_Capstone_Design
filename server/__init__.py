"""
AGV Server 패키지 — TU Capstone Design AGV 물류 피킹 시스템.

이 파일의 역할 = 패키지 공개 API 정의(re-export).
하위 디렉토리(comm/core/planning/managers/data)에 흩어진 핵심 클래스를
패키지 최상위로 끌어올려, 외부에서 짧은 경로로 import할 수 있게 한다.

  # 이 re-export 덕분에 아래처럼 짧게 가능:
  from server import RequestHandler, Config
  # (원경로 from server.core.request_handler import RequestHandler 대신)

시스템 개요:
  - MQTT: AGV ↔ 서버 (마커 보고 수신 / 개별 명령 발행) + GUI ↔ 서버 (주문/도착 알림)
  - A* 시공간 경로 계획 (선반 노드 통과 제외 + reservation 기반 충돌/교착 예방)
  - 선반 운반 + 다단계 피킹 워크플로우 (포워딩/인터셉트/스테이징)
"""

# ─── 공개 클래스 re-export (각 하위 모듈에서 끌어올림) ───
from .config import Config                          # 전역 설정
from .planning.path_planner import PathPlanner      # A* 시공간 경로 계획
from .comm.mqtt_client import MQTTClient            # MQTT publish/subscribe
from .managers.robot import RobotManager            # 로봇 6단계 상태 머신
from .managers.shelf import ShelfManager            # 선반 3상태 추적
from .managers.task import TaskManager              # 주문 → 서브태스크 분해
from .core.request_handler import RequestHandler    # 두뇌: 이벤트 → 결정 → 명령 (3 mixin)

# `from server import *` 시 노출할 이름 (공개 API 명시)
__all__ = [
    "Config",
    "PathPlanner",
    "MQTTClient",
    "RobotManager",
    "ShelfManager",
    "TaskManager",
    "RequestHandler",
]
