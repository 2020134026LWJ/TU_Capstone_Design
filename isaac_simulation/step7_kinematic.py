"""
Step 7 — Kinematic Physics

step6_visual.py 기반 변경사항:
  1. VisualCuboid -> DynamicCuboid (AGV 바디): 물리 충돌 감지 등록
  2. 선반 루트에 RigidBodyAPI(kinematic) + 보이지 않는 collision box 추가
  3. 바닥에 CollisionAPI 추가 (정적 충돌체)
  이동 로직(xformOp:translate Set)은 step6와 동일 유지

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh \\
        /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py
"""

from isaacsim import SimulationApp
simulation_app = SimulationApp({"headless": False})

import carb
import carb.input
carb.settings.get_settings().set("/persistent/app/viewport/camLightEnabled", True)
# 배경 흰색
carb.settings.get_settings().set("/rtx/backgroundColorEnabled", True)
carb.settings.get_settings().set("/rtx/backgroundColor", [0.10, 0.12, 0.16, 1.0])

import json
import os
import sys
import time
import numpy as np

# TU_Capstone_Design/ 루트를 sys.path에 추가 → hardware/ 패키지 공유
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from pxr import UsdGeom, UsdShade, UsdLux, Sdf, Vt, Gf, UsdPhysics
import omni.usd
import omni.appwindow

from isaacsim.core.api import World
from isaacsim.core.api.objects import VisualCuboid, VisualCylinder, VisualSphere, DynamicCuboid
from isaacsim.core.utils.viewports import set_camera_view

from isaac_hw import IsaacMotors
from bridge_isaac import Bridge
from camera import IsaacCamera

# ─── CAD 경로 설정 ────────────────────────────────────────────────────────────
# None     = 기본 도형 사용 (현재)
# 경로 지정 = USD 파일 로드 (CAD 파일 완성 후 여기만 변경)
#
# 예시:
#   "agv":   "/path/to/agv_body.usd"   — AGV 본체+바퀴+리프트 일체형 USD
#   "shelf": "/path/to/shelf.usd"      — 선반 USD
#   "workstation": "/path/to/ws.usd"   — 작업대 USD
CAD_PATHS = {
    "agv":         None,
    "shelf":       None,
    "workstation": None,
}

# ─── 이동/리프트 파라미터 ─────────────────────────────────────────────────────
MOVE_SPEED         = 1.5    # m/s (직진 시 각 바퀴 속도)
POSITION_TOLERANCE = 0.05   # m
LIFT_SPEED         = 0.3    # m/s

# ─── 차동구동 파라미터 (Step 6) ───────────────────────────────────────────────
WHEEL_RADIUS    = 0.06   # m  (바퀴 반경)
WHEEL_BASE      = 0.44   # m  (좌우 바퀴 간격: 2 × 0.22)
HEADING_SPEED   = 6.0    # rad/s (목표 제자리 회전 속도)
# TURN_SPEED: 제자리 회전 시 각 바퀴 속도 = HEADING_SPEED * WHEEL_BASE / 2
TURN_SPEED      = HEADING_SPEED * WHEEL_BASE / 2.0   # 1.32 m/s
ANGLE_TOLERANCE = 0.03   # rad (방향 전환 완료 임계값)

# ─── 카메라 파라미터 ──────────────────────────────────────────────────────────
CAM_HEIGHT        = 0.15
DETECT_INTERVAL   = 5
CAM_DETECT_RADIUS = 0.087  # m

# ─── 디지털 트윈 모드 ─────────────────────────────────────────────────────────
# TWIN=1 로 실행하면 Isaac이 'AGV 역할'을 그만두고 '관찰자'가 된다.
#   일반(TWIN=0): Isaac이 곧 AGV — 가상 카메라로 마커를 발행하고 cmd_ack도 보낸다.
#   트윈(TWIN=1): 실물 AGV(라파)가 마커를 발행. Isaac은 아무것도 발행하지 않고
#                 서버의 /agv/cmd + 실물의 /agv/marker를 구독해 똑같이 움직인다.
#                 → 발행자가 둘이 되면 서버가 같은 로봇의 도착을 두 번 받으므로 발행 금지.
# 서버는 실물의 마커 보고가 와야 다음 명령을 내리므로, Isaac이 먼저 도착해도
# 그 노드에서 기다린다 → 별도 동기화 장치 없이 실물과 보조가 맞는다.
TWIN_MODE = os.environ.get("TWIN", "0") == "1"

# 트윈 페이싱 (수정 60) — 실물과 보조를 맞춘다.
#   문제: 트윈도 /agv/cmd 를 받아 자기 속도(MOVE_SPEED)로 움직인다. 실물보다 빠르면 먼저
#         도착해 멈춰 서 있고, 느리면 마커가 와서 순간이동한다 → 한 칸마다 어긋난다.
#   해결: (1) 한 칸을 실물이 몇 초에 가는지 실측(forward 발행 → 마커 도착)해서 그 시간에
#         맞춰 시뮬 속도를 정하고, (2) 엣지 끝까지 가지 않고 HOLD_RATIO 지점에서 멈춰
#         실물 마커를 기다린다. 실물이 도착해야 트윈도 노드에 안착한다.
# 첫 칸 추정값 — 실측이 1회 들어오면 EMA가 바로 끌고 간다. 즉 "첫 칸만" 이 값으로 달린다.
# 실물 1칸 시간을 이미 알면 실행 시 넘겨라:  TWIN=1 TWIN_EDGE_SECS=3.2 python.sh ...
# (로그의 "실측 1칸 N초"가 안정되면 그 값을 기본값으로 박아두면 첫 칸도 안 어긋난다)
TWIN_EDGE_SECS_INIT = float(os.environ.get("TWIN_EDGE_SECS", "4.0"))
# 회전·리프트도 같은 방식으로 페이싱한다. 완료 신호가 마커가 아니라 cmd_ack라는 것만 다르다
# (명령 발행 → 실물의 cmd_ack 도착 = 실물이 그 동작에 쓴 시간).
TWIN_TURN_SECS_INIT = float(os.environ.get("TWIN_TURN_SECS", "1.0"))
TWIN_LIFT_SECS_INIT = float(os.environ.get("TWIN_LIFT_SECS", "2.0"))
TWIN_EMA_ALPHA      = 0.4    # 실측 반영 비율 (0=고정, 1=직전값만)
# 엣지의 이 지점까지만 가고 실물 마커를 기다린다. 1.0 = 노드까지 완전히 가서 기다린다.
TWIN_HOLD_RATIO     = float(os.environ.get("TWIN_HOLD_RATIO", "1.0"))
TWIN_SPEED_MIN      = 0.05   # m/s — 페이싱 속도 하한 (실물이 오래 멈춰도 기어가지 않게)

# 수정 65 — 페이싱을 '일부러 빠른 쪽으로' 편향한다.
#
# 보간하는 이상 추정은 반드시 틀린다. 그러면 문제는 "안 틀리는 법"이 아니라
# **"어느 방향으로 틀릴 것인가"** 다. 실패 모양이 둘인데 무게가 전혀 다르다:
#   · 너무 빠름 → 트윈이 먼저 도착해 **기다린다**.  사람 눈: "잠깐 멈췄네" (자연스러움)
#   · 너무 느림 → 마커가 왔는데 트윈은 엣지 중간 → **순간이동으로 끌려간다**.
#                                                 사람 눈: "고장났네" (신뢰 붕괴)
# 게다가 순간이동은 **정보를 파괴한다** — 언제 얼마나 틀렸는지가 그냥 사라진다.
# 반면 기다림은 **대기 시간이 곧 오차의 크기**라 그대로 읽을 수 있다.
# 그래서 실측 평균을 그대로 쓰지 않고 이 비율만큼 짧게 잡아 항상 조금 일찍 닿게 한다.
TWIN_SPEED_BIAS     = float(os.environ.get("TWIN_SPEED_BIAS", "0.85"))

# 먼저 도착해 기다리는 게 이 시간을 넘으면 경고한다.
#
# [주의] 일찍 도착하는 것 자체가 위험을 하나 만든다: **실물이 중간에 끼어 멈춰도 트윈은
# 목적지에 얌전히 서 있어서 멀쩡해 보인다.** 트윈의 값어치는 '예쁨'이 아니라 '정직함'인데
# (실제로 오늘 트윈이 서버의 heading 오류를 잡아냈다), 매끄럽게 보이려다 그걸 잃으면 안 된다.
# → 먼저 도착하되, **기다리고 있다는 사실을 로그로 드러낸다.**
TWIN_WAIT_WARN_SECS = float(os.environ.get("TWIN_WAIT_WARN", "5.0"))
# 도착 스냅이 이만큼 넘게 튀면 경고 — "추정이 틀렸다" 또는 "서버가 현실과 어긋났다"는 신호.
TWIN_SNAP_WARN_M    = 0.30

# 수정 72 — 1칸 주행 시간의 물리적 상한 [s]. 이걸 넘으면 '주행'이 아니라 '대기'로 보고
# 학습에서 제외한다. 실물 AGV가 1m를 이보다 오래 걸릴 이유가 없다.
# (실물이 정말 더 느리면 이 값을 올려라. 카메라 벤치처럼 사람이 마커를 드는 모드에서는
#  이 상한이 트윈을 사람 반응속도로 끌어내리지 않게 막아준다.)
TWIN_EDGE_MAX = float(os.environ.get("TWIN_EDGE_MAX", "8.0"))

# 수정 68 (B) — 회전 실시간 추종.
#
# **직진과 회전은 비대칭이다.** 직진 중엔 마커가 시야를 벗어나 보간이 불가피하지만,
# 회전은 노드 위에서 제자리로 도니 **발밑 마커가 계속 보인다** → 추정이 아니라 **측정**이 가능하다.
# 그래서 회전만 /agv/pose(카메라 yaw)로 직접 따라간다. 직진은 그대로 시간 보간(수정 60/65).
#
# 마커가 시야에서 사라지면 pose가 끊긴다 → 이 시간을 넘으면 다시 시간 보간으로 되돌아간다.
POSE_STALE_SECS = 0.5

# ─── 설정 파일 경로 ───────────────────────────────────────────────────────────
MAP_PATH   = os.path.join(_ROOT, "server", "data", "map.json")
SHELF_PATH = os.path.join(_ROOT, "server", "data", "shelf_config.json")
ROBOT_PATH = os.path.join(_ROOT, "server", "data", "robot_config.json")
ARUCO_DIR  = os.path.join(_ROOT, "isaac_simulation", "aruco_markers")

with open(MAP_PATH)   as f: map_cfg   = json.load(f)
with open(SHELF_PATH) as f: shelf_cfg = json.load(f)
with open(ROBOT_PATH) as f: robot_cfg = json.load(f)

nodes          = {n["id"]: n for n in map_cfg["nodes"]}
shelf_node_ids = set(map_cfg["shelf_nodes"])
ws_node_ids    = set(map_cfg["workstation_nodes"])
robot_homes    = {int(rid): info["home_node"]
                  for rid, info in robot_cfg["robots"].items()}

# 양방향 인접 그래프 (forward 명령 시 다음 노드 탐색용)
adjacency: dict[int, list[int]] = {}
for _e in map_cfg["edges"]:
    _a, _b = int(_e["from"]), int(_e["to"])
    adjacency.setdefault(_a, []).append(_b)
    adjacency.setdefault(_b, []).append(_a)


def node_xy(node_id: int) -> np.ndarray:
    n = nodes[node_id]
    return np.array([n["x"], n["y"]], dtype=float)


def server_deg_to_rad(deg: float) -> float:
    """서버 heading(0=N/90=E/180=S/270=W) → Isaac heading(라디안, 0=E/π/2=N).

    camera.py의 역변환: heading_deg = (90 - degrees(rad)) % 360
    """
    return np.radians((90.0 - deg) % 360.0)


# 트윈 모드 초기 heading — 서버가 '실물의 첫 마커'에서 가정하는 값과 반드시 같아야 한다.
# 실물 라파는 heading을 안 보낸다(옵션 a) → 서버는 robot.heading 기본값 0°(북)를 유지.
# 여기서 안 맞추면 서버는 '북쪽을 본다'고 믿고 회전을 계산하는데 Isaac 몸체는 동쪽을
# 보고 있어, 같은 명령이 서로 다른 노드로 향한다 (트윈이 엉뚱한 데로 감).
TWIN_INIT_HEADING_DEG = 0    # 서버 RobotManager의 heading 기본값과 일치

# 수정 71 — 트윈을 **서버 도중에** 붙일 때 AGV의 실제 위치를 명시한다.
#   TWIN_START_NODE="1:19,2:27"  → AGV-1은 19번, AGV-2는 27번에 있다
# 안 주면 홈 노드에서 시작한다(서버와 동일 전제).
#
# 예전엔 "첫 마커는 무조건 믿는다"로 때웠는데, 유령 마커가 첫 마커로 들어오면
# 트윈이 그대로 순간이동해버렸다(실측: 마커 37 → 5m 점프). **추측 대신 명시한다.**
def _parse_twin_start_nodes() -> dict[int, int]:
    raw = os.environ.get("TWIN_START_NODE", "").strip()
    out: dict[int, int] = {}
    for part in raw.split(","):
        if ":" in part:
            r, n = part.split(":", 1)
            try:
                out[int(r)] = int(n)
            except ValueError:
                pass
    return out


TWIN_START_NODES = _parse_twin_start_nodes()


def _normalize_angle(a: float) -> float:
    while a >  np.pi: a -= 2.0 * np.pi
    while a < -np.pi: a += 2.0 * np.pi
    return a


# ═══════════════════════════════════════════════════════════════════════════════
# IsaacAGV — 이동(차동구동) + 리프트 + 시각 상태머신
# ═══════════════════════════════════════════════════════════════════════════════

class IsaacAGV:
    BODY_Z         = 0.10
    WHEEL_Z        = 0.06
    SCISSOR_Z      = 0.195
    LIFT_PLATE_Z   = 0.25
    LIFT_PLATE_UP  = 0.42
    LIFT_CONTACT_Z = 0.3775
    WHEEL_OFFSETS  = [(0.0, +0.22), (0.0, -0.22)]

    def __init__(self, rid: int, home_node: int):
        self.rid          = rid
        # 수정 71: 트윈을 서버 도중에 붙였다면 TWIN_START_NODE로 실제 위치를 받는다.
        self.current_node = TWIN_START_NODES.get(rid, home_node)

        # ── 모터 드라이버 (교체 포인트) ──────────────────────────────────────
        # 실물 전환 시: IsaacMotors() → RaspiMotors()
        #   from hardware.raspi_hw import RaspiMotors
        #   self.motors = RaspiMotors()
        self.motors = IsaacMotors()

        # ── 이동 상태 ──────────────────────────────────────────────────────
        # IDLE / TURNING / MOVING
        self.state            = "IDLE"
        # 일반 모드: 0 rad(동). Isaac이 자기 heading을 마커와 함께 발행 → 서버가 90°로 맞춤.
        # 트윈 모드: 실물은 heading을 안 보내 서버가 0°(북)로 가정 → 몸체도 북을 봐야 일치.
        self.heading          = (server_deg_to_rad(TWIN_INIT_HEADING_DEG)
                                 if TWIN_MODE else 0.0)   # 라디안, 0=East, π/2=North
        self.heading_target   = self.heading   # TURNING 목표 방향

        # 수정 71: 시작 위치도 current_node를 따른다 (TWIN_START_NODE 지정 시 그곳).
        # 여기서 home_node를 그대로 쓰면 좌표만 홈에 남아 노드 번호와 어긋난다.
        home = node_xy(self.current_node)
        self.pos             = home.copy()
        self.target_pos      = home.copy()
        self._moving_to_node = home_node

        # ── 리프트 ────────────────────────────────────────────────────────
        self.lift_z         = self.LIFT_PLATE_Z
        self.lift_target_z  = self.LIFT_PLATE_Z
        self.lift_state     = "IDLE"
        self.carrying_shelf = None
        self.shelf_offset   = Gf.Quatf(1.0, Gf.Vec3f(0.0, 0.0, 0.0))  # identity

        # ── 시각 (Step 6) ─────────────────────────────────────────────────
        self.wheel_angle = 0.0  # 이동 거리 기반 누적 회전각

        # ── CAD 모드 여부 ─────────────────────────────────────────────────
        # CAD_PATHS["agv"] 지정 시 True → _sync_prim 이 루트 prim 만 이동
        self._use_cad = bool(CAD_PATHS.get("agv"))

        # ── Bridge / Camera (교체 포인트) ─────────────────────────────────
        # bridge: Bridge 인스턴스 — publish_cmd_ack / publish_marker 사용
        # camera: IsaacCamera 인스턴스 — detect() 사용
        # set_bridge() 로 초기화됨
        self.bridge = None  # Bridge
        self.camera = None  # IsaacCamera

        self._last_marker: int | None = None

        # MQTT 스레드 → main loop 핸드오프
        self._pending_cmd: str | None = None   # 실행할 다음 명령
        self._pending_shelf_id: int | None = None  # lift_up 대상 선반 (서버 지정, 약점 3)
        self._pending_target_node: int | None = None  # forward 도착 노드 (서버 지정, 수정 70)

        # 수정 73 — 실물이 붙었는가 (트윈 모드). 붙기 전엔 화면에 안 그린다.
        self._twin_seen    = False   # MQTT로 이 rid의 신호를 받은 적 있나
        self._twin_visible = False   # 화면에 그려져 있나
        self._pending_marker: int | None = None    # 트윈: 실물이 보고한 마커 (동기화 대기)
        self._current_turn_cmd: str | None = None  # 실행 중인 turn 명령 이름

        # ── 트윈 페이싱 (수정 60) ─────────────────────────────────────────
        # 직진: forward → 마커 도착으로 실측 / 회전·리프트: cmd → cmd_ack로 실측
        self._twin_edge_secs   = TWIN_EDGE_SECS_INIT  # 실측 1칸 소요시간 (EMA)
        self._twin_edge_n      = 0                    # 실측 횟수
        self._twin_forward_t: float | None = None     # forward 실행 시각 (실측 시작점)
        self._twin_hold_pos = None                    # 홀드 지점 (엣지 끝)
        self._twin_holding  = False                   # 홀드 중 = 실물 마커 대기
        self._twin_hold_t: float | None = None        # 홀드 진입 시각 (대기 시간 = 추정 오차)

        # 수정 68 — 실물 연속 자세 (/agv/pose). 회전을 실시간으로 따라가는 데 쓴다.
        self._pose_latest = None    # (marker_id, yaw_deg, 수신시각) — MQTT 스레드가 갱신
        self._pose_ref    = None    # (marker_id, yaw 기준, heading 기준) — 회전량 계산 원점
        self._pose_log_t  = 0.0     # 추종 로그 rate limit
        self._move_speed    = MOVE_SPEED              # 이번 엣지에 쓸 속도

        self._twin_turn_secs = TWIN_TURN_SECS_INIT    # 실측 회전 소요시간 (EMA)
        self._twin_turn_n    = 0
        self._twin_lift_secs = TWIN_LIFT_SECS_INIT    # 실측 리프트 소요시간 (EMA)
        self._twin_lift_n    = 0
        self._twin_cmd_t: float | None = None         # turn/lift 실행 시각 (실측 시작점)
        self._twin_turn_total  = 0.0                  # 이번 회전의 총 각도 (rad)
        self._twin_lift_total  = 0.0                  # 이번 리프트의 총 높이 (m)
        self._turn_wheel_speed = TURN_SPEED           # 이번 회전에 쓸 바퀴 속도
        self._lift_speed       = LIFT_SPEED           # 이번 리프트에 쓸 속도
        self._twin_holding_turn = False               # 회전 홀드 = 실물 cmd_ack 대기
        self._twin_holding_lift = False               # 리프트 홀드 = 실물 cmd_ack 대기
        self._pending_ack: str | None = None           # 트윈: 실물이 보고한 cmd_ack

    def set_bridge(self, bridge):
        """Bridge 인스턴스 연결 — cmd_ack 발행 경로 설정"""
        self.bridge = bridge

    def set_camera(self, camera):
        """Camera 인스턴스 연결"""
        self.camera = camera

    def _on_cmd_from_bridge(self, rid: int, cmd: str, shelf_id: int | None = None,
                            target_node: int | None = None):
        """Bridge cmd_handler 콜백 — main loop 핸드오프.

        shelf_id   : lift 대상 선반 (약점 3)
        target_node: forward 도착 예정 노드 (수정 70) — 서버가 알려준다.
                     예전엔 트윈이 자기 heading으로 추측했는데, heading이 서버와 갈리면
                     같은 forward를 다른 목적지로 해석해 교착이 났다.
        """
        self._pending_cmd = cmd
        self._pending_shelf_id = shelf_id
        self._pending_target_node = target_node
        self._twin_seen = True      # 서버가 이 AGV에게 명령을 냈다 = 존재한다

    def poll_camera(self):
        """카메라 감지 → marker 보고 (main loop에서 호출)"""
        if TWIN_MODE:
            return          # 트윈: 마커는 실물이 보고한다 (이중 발행 금지)
        if self.camera is None or self.bridge is None:
            return
        if self.state not in ("MOVING", "IDLE"):
            return
        marker_id, heading_deg = self.camera.detect()
        if marker_id is not None:
            self.bridge.publish_marker(marker_id, heading_deg)

    def _ack(self, cmd: str, *shelf_id):
        """cmd_ack 발행 — 트윈 모드에선 실물이 보고하므로 침묵한다.

        shelf_id는 그대로 통과시킨다(가변인자). lift의 shelf_id=None은 '빈 리프트'라는
        의미 있는 값이라 생략과 구분돼야 한다 (약점 4 — 서버가 선반 분실을 감지).
        """
        if TWIN_MODE or self.bridge is None:
            return
        self.bridge.publish_cmd_ack(cmd, *shelf_id)

    def _on_marker_from_real(self, rid: int, marker_id: int):
        """트윈 모드 — 실물 AGV의 마커 보고 수신 (MQTT 스레드). main loop로 핸드오프."""
        self._pending_marker = marker_id
        self._twin_seen = True      # 실물이 마커를 보고했다 = 연결됨 (수정 73)

    def _on_ack_from_real(self, rid: int, cmd: str):
        """트윈 모드 — 실물의 회전/리프트 완료(cmd_ack) 수신. main loop로 핸드오프."""
        self._pending_ack = cmd
        self._twin_seen = True

    def _on_pose_from_real(self, rid: int, marker_id: int, yaw_deg: float):
        """트윈 모드 — 실물의 연속 자세(/agv/pose) 수신 (수정 68). main loop가 소비."""
        self._pose_latest = (marker_id, float(yaw_deg), time.time())
        self._twin_seen = True

    def _apply_pose_heading(self) -> bool:
        """실물 카메라 yaw로 heading을 **직접** 갱신. 적용했으면 True.

        **회전 중에만 쓴다.** 직진 중엔 로봇이 돌지 않으므로 yaw 변화는 노이즈일 뿐이고,
        그걸 heading에 먹이면 트윈이 슬금슬금 틀어진다.

        yaw는 '마커 대비 상대 각도'라 절대값은 못 믿는다(HEADING_OFFSET 미확정).
        하지만 **변화량은 믿을 수 있다** — 같은 마커를 보는 동안의 yaw 변화 = 실제 회전량.
        그래서 마커가 바뀌면 기준을 다시 잡는다.

        부호: 실측(2026-07-12) **카메라가 시계방향으로 돌면 yaw 증가**.
        트윈 내부 heading은 수학 규약(0=East, 반시계 +)이라 **시계방향 = heading 감소** → 뺀다.
        """
        if not TWIN_MODE or self._pose_latest is None:
            return False
        mid, yaw, ts = self._pose_latest
        if time.time() - ts > POSE_STALE_SECS:
            return False                    # 마커가 시야에서 사라짐 → 시간 보간으로 폴백

        if self._pose_ref is None or self._pose_ref[0] != mid:
            self._pose_ref = (mid, yaw, self.heading)   # 마커 바뀜 → 기준 재설정
            return False

        _, yaw_ref, head_ref = self._pose_ref
        d_cw = np.radians((yaw - yaw_ref + 180.0) % 360.0 - 180.0)   # 시계방향 회전량
        self.heading = _normalize_angle(head_ref - d_cw)

        # 추종하고 있다는 걸 눈으로 확인할 창구 (0.5초에 한 번).
        # 없으면 "시간 보간으로 돌았는지 실물을 따라갔는지" 구분할 방법이 없다.
        now = time.time()
        if now - self._pose_log_t > 0.5:
            self._pose_log_t = now
            print(f"[AGV {self.rid}] (트윈) 회전 추종 — 실물 yaw {yaw:.0f}° "
                  f"(기준 {yaw_ref:.0f}°, 시계 {np.degrees(d_cw):+.0f}°) "
                  f"→ heading {np.degrees(self.heading) % 360:.0f}°")
        return True

    # ─── 트윈 페이싱 (수정 60) ───────────────────────────────────────────────

    def _start_twin_pacing(self):
        """forward 시작 — 실측 시간에 맞춰 이번 엣지 속도를 정하고 홀드 지점을 잡는다."""
        # 수정 65: 출발점을 진실로 맞춘다(원점 리셋).
        #
        # 동기화는 두 번 하고 **역할이 다르다**:
        #   · 출발 시(여기)  = 보간이 시작되는 원점을 노드에 정확히 박는다
        #                      → 오차가 다음 엣지로 **누적되지 않는다**
        #   · 도착 시(sync_to_node) = 최종 위치를 진실로 맞춘다
        # 정상 상황에선 이미 노드에 있으므로 아무 일도 안 일어난다(공짜).
        # 뭔가 어긋나 있었다면 여기서 조용히 바로잡힌다.
        drift = float(np.linalg.norm(self.pos - node_xy(self.current_node)))
        if drift > 1e-3:
            print(f"[AGV {self.rid}] (트윈) 출발 동기화 — 노드 {self.current_node}에서 "
                  f"{drift:.3f}m 어긋나 있었음")
        self.pos = node_xy(self.current_node).copy()

        edge = self.target_pos - self.pos
        edge_len = float(np.linalg.norm(edge))
        if edge_len < 1e-6:
            return
        # 실측 1칸 소요시간에 맞춘 속도 (아직 실측 전이면 INIT 추정값).
        # BIAS를 곱해 **일부러 빠르게** → 항상 조금 먼저 도착해서 기다린다 (순간이동 방지).
        paced_secs = self._twin_edge_secs * TWIN_SPEED_BIAS
        self._move_speed    = max(TWIN_SPEED_MIN,
                                  min(MOVE_SPEED, edge_len / paced_secs))
        self._twin_hold_pos = self.pos + edge * TWIN_HOLD_RATIO
        self._twin_holding  = False
        self._twin_hold_t   = None       # 홀드 진입 시각 (대기 시간 = 추정 오차)
        self._twin_forward_t = time.time()

    def _start_twin_turn_pacing(self):
        """turn 시작 — 실측 회전시간에 맞춰 각속도를 정한다."""
        # 수정 68: **회전을 시작할 때마다 pose 기준을 다시 잡는다.**
        #
        # yaw는 '마커 대비 상대 각도'라 절대값을 못 믿는다. 그래서 (yaw 기준, heading 기준)
        # 쌍을 잡아두고 그 차이로 회전량을 만든다. 그런데 그 기준을 **회전이 끝난 뒤에도
        # 그대로 두면** 다음 회전이 낡은 기준 위에서 계산된다.
        # (실측 버그: 1차 회전 후 heading이 목표로 스냅됐는데 기준은 안 바뀌어서,
        #  2차 회전이 28° 틀어진 채로 시작했다. 회전할수록 오차가 쌓인다.)
        #
        # 회전 직전이 재기준을 잡기 가장 좋은 시점이다 — 이때 트윈 heading은 확실히 맞다
        # (직전 동작이 끝나 스냅/동기화된 상태).
        #
        # 이미 손에 있는 최신 pose로 **즉시** 기준을 잡는다. 다음 pose를 기다리면 그 사이
        # 트윈이 시간 적분으로 자유주행해 그만큼 어긋난 채로 기준이 잡힌다.
        self._pose_ref = None
        if self._pose_latest is not None:
            mid, yaw, ts = self._pose_latest
            if time.time() - ts <= POSE_STALE_SECS:
                self._pose_ref = (mid, yaw, self.heading)   # 지금 heading이 정답

        total = abs(_normalize_angle(self.heading_target - self.heading))
        if total < 1e-6:
            total = np.pi          # turn_180 (diff가 ±π라 부호에 따라 0에 가까울 수 있음)
        self._twin_turn_total = total
        omega = total / max(0.1, self._twin_turn_secs)      # rad/s
        self._turn_wheel_speed = omega * WHEEL_BASE / 2.0   # 차동구동 바퀴 속도
        self._twin_holding_turn = False
        self._twin_cmd_t = time.time()

    def _start_twin_lift_pacing(self):
        """lift 시작 — 실측 리프트시간에 맞춰 승강 속도를 정한다."""
        total = abs(self.lift_target_z - self.lift_z)
        self._twin_lift_total = total
        self._lift_speed = total / max(0.1, self._twin_lift_secs)   # m/s
        self._twin_holding_lift = False
        self._twin_cmd_t = time.time()

    def on_real_ack(self, cmd: str, stage):
        """트윈 — 실물의 cmd_ack 수신 (main loop). 실측 + 홀드 중인 동작을 끝낸다."""
        is_turn = cmd in ("turn_left", "turn_right", "turn_180")
        is_lift = cmd in ("lift_up", "lift_down")

        # 실측 (명령 발행 → ack 도착 = 실물이 그 동작에 쓴 시간)
        if self._twin_cmd_t is not None and (is_turn or is_lift):
            secs = time.time() - self._twin_cmd_t
            self._twin_cmd_t = None
            if 0.1 < secs < 120.0:
                if is_turn:
                    self._twin_turn_secs = (TWIN_EMA_ALPHA * secs
                                            + (1 - TWIN_EMA_ALPHA) * self._twin_turn_secs)
                    self._twin_turn_n += 1
                    print(f"[AGV {self.rid}] (트윈) 실측 회전 {secs:.2f}초 → 평균 "
                          f"{self._twin_turn_secs:.2f}초 (n={self._twin_turn_n})")
                else:
                    self._twin_lift_secs = (TWIN_EMA_ALPHA * secs
                                            + (1 - TWIN_EMA_ALPHA) * self._twin_lift_secs)
                    self._twin_lift_n += 1
                    print(f"[AGV {self.rid}] (트윈) 실측 리프트 {secs:.2f}초 → 평균 "
                          f"{self._twin_lift_secs:.2f}초 (n={self._twin_lift_n})")

        # 홀드 해제 → 동작 완료 (실물보다 먼저 끝내지 않는다)
        if is_turn and self.state == "TURNING":
            self._twin_holding_turn = False
            self.heading = self.heading_target
            self._finish_turn(stage)
        elif is_lift and self.lift_state != "IDLE":
            self._twin_holding_lift = False
            self.lift_z = self.lift_target_z
            self._finish_lift(stage)

    def _record_twin_edge(self, nid: int):
        """마커 도착 — 실물이 한 칸을 몇 초에 갔는지 실측해 EMA로 반영."""
        if self._twin_forward_t is None or nid != self._moving_to_node:
            return                      # 예상 밖 노드(드리프트) → 측정 안 함
        secs = time.time() - self._twin_forward_t
        self._twin_forward_t = None

        # 수정 72 — **물리적으로 불가능한 값은 배우지 않는다.**
        #
        # 예전 상한은 120초라 사실상 없는 것과 같았다. 그래서 '주행 시간'이 아닌 것까지
        # 학습했다: 카메라 벤치에서 사람이 카드를 바꿔 드는 시간(9~12초)을 1칸 주행으로
        # 배워 트윈이 기어갔다(실측 평균 9.89초/칸).
        # 실물에서도 회랑 대기·사람 피킹으로 마커가 늦으면 같은 일이 난다 —
        # **한 번 배우면 그 뒤 모든 칸이 느려진다.**
        #
        # 1m를 TWIN_EDGE_MAX 넘게 걸렸다면 그건 주행이 아니라 **대기**다.
        # 측정을 버리고 기존 추정을 유지한다 → 트윈은 정상 속도로 움직이고,
        # 늦게 온 마커는 그냥 "오래 기다렸다"가 된다(대기 로그로 드러남).
        if secs > TWIN_EDGE_MAX:
            print(f"[AGV {self.rid}] (트윈) 1칸 {secs:.1f}초 — 주행이 아니라 대기로 보고 "
                  f"학습 제외 (상한 {TWIN_EDGE_MAX:.0f}초, 평균 {self._twin_edge_secs:.2f}초 유지)")
            return
        if secs < 0.2:                  # 중복 마커 등 이상치
            return
        self._twin_edge_secs = (TWIN_EMA_ALPHA * secs
                                + (1.0 - TWIN_EMA_ALPHA) * self._twin_edge_secs)
        self._twin_edge_n += 1
        print(f"[AGV {self.rid}] (트윈) 실측 1칸 {secs:.2f}초 "
              f"→ 평균 {self._twin_edge_secs:.2f}초 (n={self._twin_edge_n})")

    def _heal_heading(self, nid: int):
        """트윈 방향 자가복구 — 실물이 지나온 두 노드로 실제 heading을 역산한다.

        왜 필요한가: 트윈을 서버 도중에 켜면 트윈은 자기가 홈 노드·heading 0이라고 믿는다.
        실제 로봇은 다른 곳에서 다른 방향을 보고 있다 → forward 목표 노드를 엉뚱하게
        골라 헤매다 마커가 올 때마다 멀리 순간이동한다.

        실물은 자기 heading 방향으로만 직진하므로 **직전 노드 → 지금 노드 벡터 = 실제 heading**.
        마커 하나만 더 받으면 정렬된다. (인접 노드일 때만 — 순간이동한 보고는 신뢰 못 함)
        """
        prev = self.current_node
        if prev is None or prev == nid or prev not in nodes:
            return
        (px, py), (cx, cy) = node_xy(prev), node_xy(nid)
        dx, dy = float(cx - px), float(cy - py)
        if abs(dx) + abs(dy) > 1.01:      # 인접(1칸)이 아니면 방향을 못 믿는다
            return
        real_heading = _normalize_angle(float(np.arctan2(dy, dx)))
        if abs(_normalize_angle(real_heading - self.heading)) > 0.1:
            print(f"[AGV {self.rid}] (트윈) 방향 보정 {np.degrees(self.heading):.0f}° → "
                  f"{np.degrees(real_heading):.0f}° (실물 이동 {prev}→{nid} 기준)")
            self.heading = real_heading

    def sync_to_node(self, nid: int, stage):
        """트윈 모드 — 실물이 보고한 노드로 위치 보정 (main loop에서 호출).

        회전/리프트 중에는 건드리지 않는다 (그 동작은 명령으로 이미 재현 중).

        [주의] pos만 바꾸면 화면은 그대로다. _sync_prim은 _update_move가 MOVING/TURNING일
        때만 부르는데, 트윈은 마커를 받고 곧장 IDLE이라 그 경로를 안 탄다 → 여기서 직접
        호출해야 USD prim(차체·바퀴·선반)이 새 위치로 그려진다.
        """
        if nid not in nodes:
            print(f"[AGV {self.rid}] (트윈) 알 수 없는 마커 {nid} — 무시")
            return

        # 수정 66: 서버와 같은 판단 기준을 트윈에도 적용한다 (수정 62/64와 동일 논리).
        #
        # 왜: 지금까지 **서버는 이상한 마커를 거부하는데 트윈은 그냥 믿었다.**
        #   서버: 불가능한 마커 3 무시 (현재 10, 이웃 [9,11,2,18])
        #   트윈: 실물 마커 3 → 위치 동기화          ← 따라가버림
        # 그 순간부터 둘은 다른 위치를 믿는다. 트윈은 현실도 아니고 서버 생각도 아닌
        # 아무것도 아닌 걸 그리게 된다. 실물 카메라가 마커를 한 번 잘못 읽으면 바로 이 상태다.
        #
        # "로봇은 한 칸씩 굴러가지 순간이동하지 않는다"는 서버만의 상식이 아니라 **물리 법칙**이다.
        # 트윈도 물리적으로 불가능한 보고는 믿지 않아야 한다.
        # [수정 71] 수정 66의 "첫 마커는 무조건 수용" 예외를 **제거한다.**
        #
        # 그 예외는 실측에서 곧바로 악용당했다. 서버 켜자마자 유령 마커 37(오검출,
        # x=-212mm = 화면 가장자리)이 들어오자:
        #   서버:  "불가능한 마커 37 무시 (현재 9)"   ← 막았다
        #   트윈:  "첫 마커 37 — 위치 확정"           ← 받아들여 5m 순간이동
        #
        # 서버에는 이 예외를 **일부러 안 넣었다**(같은 시나리오를 알고 있었으니까).
        # 그런데 트윈에는 "제어를 안 하니 안전하다"며 넣었다 — 그게 틀렸다.
        # **제어를 안 해도 트윈이 거짓을 그리면 트윈의 존재 이유가 사라진다.**
        #
        # 트윈도 자기 홈 노드를 안다(robot_config). 서버와 똑같이 **처음부터** 인접성을 건다.
        # 서버 도중에 트윈을 붙이는 경우는 TWIN_START_NODE 로 **명시**한다(추측하지 않는다).
        if self.state == "MOVING" and self._moving_to_node is not None:
            # forward 중이면 도착할 수 있는 곳은 목표 노드 하나뿐 (수정 64와 동일)
            if nid != self._moving_to_node:
                print(f"[AGV {self.rid}] (트윈) 목표와 다른 마커 {nid} 무시 "
                      f"(forward 목표={self._moving_to_node})")
                return
        elif nid != self.current_node and nid not in adjacency.get(self.current_node, []):
            # 정지 중이면 현재 노드 아니면 이웃뿐 (수정 62와 동일)
            print(f"[AGV {self.rid}] (트윈) 불가능한 마커 {nid} 무시 "
                  f"(현재 {self.current_node}, 이웃 {adjacency.get(self.current_node, [])})")
            return

        self._heal_heading(nid)     # 방향 자가복구 (트윈을 중간에 켜도 한 칸이면 정렬)
        if self.state not in ("MOVING", "IDLE"):
            return
        self._record_twin_edge(nid)     # 실물 1칸 소요시간 실측 (다음 엣지 속도에 반영)

        # 수정 65: 불일치를 숨기지 말고 드러낸다.
        #   · 스냅 거리 = 트윈이 얼마나 틀린 위치에 있었나 (추정 오차 / 서버-현실 괴리)
        #   · 대기 시간 = 얼마나 일찍 도착해 기다렸나 (= 추정이 빠른 쪽으로 틀린 정도)
        # 비정상적으로 길게 기다렸다면 실물이 중간에 끼었다는 뜻이다. 그건 트윈이
        # 목적지에 얌전히 서 있어서 화면상 멀쩡해 보이므로, 로그가 유일한 창구다.
        snap = float(np.linalg.norm(node_xy(nid) - self.pos))
        waited = (time.time() - self._twin_hold_t) if self._twin_hold_t else 0.0
        # 주행 중이 아니었는데 마커가 왔다 = 트윈이 페이싱할 근거 자체가 없었다.
        # (실물로 치면 "명령도 없이 혼자 옆 칸으로 갔다"는 보고. 순간이동 외엔 그릴 방법이 없다)
        was_driving = self.state == "MOVING"

        self.pos             = node_xy(nid).copy()
        self.current_node    = nid
        self._moving_to_node = None
        self.target_pos      = None
        self.state           = "IDLE"
        self._twin_hold_pos  = None     # 홀드 해제 — 실물이 도착했으므로 노드에 안착
        self._twin_holding   = False
        self._twin_hold_t    = None
        self.motors.stop()
        self._sync_prim(stage)      # ← 화면 반영 (없으면 좌표만 바뀌고 안 움직임)
        self._sync_shelf(stage)     # 선반을 들고 있으면 같이 따라오게

        msg = f"[AGV {self.rid}] (트윈) 실물 마커 {nid} → 위치 동기화"
        if waited > 0.05:
            msg += f" (먼저 도착해 {waited:.1f}초 대기)"
        if snap > TWIN_SNAP_WARN_M:
            if not was_driving:
                # 원인이 다르다. 페이싱이 틀린 게 아니라 **페이싱할 기회가 없었다.**
                msg += (f"  ⚠ 스냅 {snap:.2f}m — forward 명령 없이 마커만 들어옴 "
                        f"(트윈은 주행 중이 아니었음 → 순간이동 외엔 방법이 없다)")
            else:
                msg += f"  ⚠ 스냅 {snap:.2f}m — 추정이 느렸거나 서버가 현실과 어긋남"
        if waited > TWIN_WAIT_WARN_SECS:
            msg += f"  ⚠ 대기 {waited:.0f}초 — 실물이 끼었을 수 있음"
        print(msg)

    # ─── 명령 실행 ───────────────────────────────────────────────────────────

    def execute_cmd(self, cmd: str, target_shelf: int | None = None,
                    target_node: int | None = None):
        """서버 명령 수신 → 상태 전환.

        target_shelf: lift_up 대상 선반 (약점 3)
        target_node : forward 도착 노드 (수정 70). **서버가 준 값을 최우선으로 쓴다** —
                      추측하지 않으면 서버와 갈릴 수가 없다. 없을 때만 heading으로 추측(폴백).
        """
        if cmd == "forward":
            target = target_node if target_node is not None else self._find_forward_target()
            if target is None:
                print(f"[AGV {self.rid}] forward: heading 방향 노드 없음 (heading={self.heading:.2f}rad)")
                return
            if target_node is not None and self.current_node is not None:
                guess = self._find_forward_target()
                if guess is not None and guess != target_node:
                    # 서버와 트윈의 heading이 갈렸다는 뜻. 서버를 따르되 드러낸다.
                    print(f"[AGV {self.rid}] (트윈) ⚠ forward 목적지 불일치 — "
                          f"서버={target_node}, 내 추측={guess} → 서버를 따름 "
                          f"(heading 장부가 서버와 갈렸다)")
            self._moving_to_node = target
            self.target_pos      = node_xy(target)
            # 출발 노드를 last_marker로 유지 → 이전 노드 재감지 방지
            # (None으로 리셋하면 출발 직후 같은 노드를 중복 감지함)
            self._last_marker = self.current_node
            if self.camera:
                self.camera.set_last_marker(self.current_node)
            self.state = "MOVING"
            if TWIN_MODE:
                self._start_twin_pacing()
            print(f"[AGV {self.rid}] <- forward → node {target}")

        elif cmd == "turn_left":
            self._current_turn_cmd = cmd
            self.heading_target    = _normalize_angle(self.heading + np.pi / 2)
            self.motors.stop()
            self.state = "TURNING"
            if TWIN_MODE:
                self._start_twin_turn_pacing()
            print(f"[AGV {self.rid}] <- turn_left")

        elif cmd == "turn_right":
            self._current_turn_cmd = cmd
            self.heading_target    = _normalize_angle(self.heading - np.pi / 2)
            self.motors.stop()
            self.state = "TURNING"
            if TWIN_MODE:
                self._start_twin_turn_pacing()
            print(f"[AGV {self.rid}] <- turn_right")

        elif cmd == "turn_180":
            self._current_turn_cmd = cmd
            self.heading_target    = _normalize_angle(self.heading + np.pi)
            self.motors.stop()
            self.state = "TURNING"
            if TWIN_MODE:
                self._start_twin_turn_pacing()
            print(f"[AGV {self.rid}] <- turn_180")

        elif cmd == "lift_up":
            # 약점 3: 서버가 지정한 선반을 직접 든다 (좌표 추측 금지).
            # teleport 방지 — 지정 선반이 실제로 근처에 있을 때만 집고, 없으면
            # 빈 리프트로 보고(약점 4). 엉뚱한 이웃 선반을 집지 않는다.
            if target_shelf is not None:
                picked = target_shelf if self._shelf_is_near(target_shelf) else None
                if picked is None:
                    print(f"[AGV {self.rid}] lift_up: 서버지정 선반 {target_shelf}이 "
                          f"근처에 없음 → 빈 리프트(보고)")
            else:
                picked = self._find_nearby_shelf()  # 폴백 (서버 미지정 / 수동)
            self.carrying_shelf = picked
            if picked is not None:
                shelf_origins.pop(picked, None)
            self.lift_target_z  = self.LIFT_PLATE_UP
            self.lift_state     = "RAISING"
            if TWIN_MODE:
                self._start_twin_lift_pacing()
            # 픽업 순간: 선반의 현재 orient와 AGV heading의 offset 저장
            # _sync_shelf에서 q_agv * shelf_offset으로 선반이 AGV와 함께 회전
            q_shelf = self._read_shelf_orient(picked)
            q_agv   = self._heading_quat(self.heading)
            q_inv   = Gf.Quatf(q_agv.GetReal(), -q_agv.GetImaginary())
            self.shelf_offset = q_inv * q_shelf
            print(f"[AGV {self.rid}] <- lift_up → shelf {picked}")

        elif cmd == "lift_down":
            self.lift_target_z = self.LIFT_PLATE_Z
            self.lift_state    = "LOWERING"
            if TWIN_MODE:
                self._start_twin_lift_pacing()
            print(f"[AGV {self.rid}] <- lift_down → shelf {self.carrying_shelf}")

    def _find_forward_target(self) -> int | None:
        """현재 heading 방향 인접 노드 탐색 (adjacency 그래프 기반)"""
        dx_h = float(np.cos(self.heading))
        dy_h = float(np.sin(self.heading))
        cx, cy = node_xy(self.current_node)

        best_node  = None
        best_score = 0.7  # 최소 정렬도 임계값

        for nid in adjacency.get(self.current_node, []):
            n = nodes[nid]
            dx = n["x"] - cx
            dy = n["y"] - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < 0.01:
                continue
            score = (dx * dx_h + dy * dy_h) / dist
            if score > best_score:
                best_score = score
                best_node  = nid

        return best_node

    def _find_nearby_shelf(self) -> int | None:
        """현재 위치 근처 선반 탐색 (lift_up 시 어떤 선반인지 결정)

        shelf_origins는 _place_shelf에서 actual 위치로 갱신되므로,
        선반이 작업대(W) 등 home 외 노드에 놓여있어도 정확히 찾음 (포워딩 재픽업).
        """
        best_nid  = None
        best_dist = POSITION_TOLERANCE * 3
        for sid, (sx, sy) in shelf_origins.items():
            dist = float(np.linalg.norm(self.pos - np.array([sx, sy])))
            if dist < best_dist:
                best_dist = dist
                best_nid  = sid
        return best_nid

    def _shelf_is_near(self, sid: int) -> bool:
        """선반 sid가 현재 AGV 위치 근처(tolerance*3)에 실제로 놓여 있는지 (약점 3).
        든 선반은 shelf_origins에서 빠져 있어 False → 중복/오발 lift_up을 빈 리프트로 처리."""
        if sid not in shelf_origins:
            return False
        sx, sy = shelf_origins[sid]
        return float(np.linalg.norm(self.pos - np.array([sx, sy]))) < POSITION_TOLERANCE * 3

    # ─── 업데이트 ────────────────────────────────────────────────────────────

    def update(self, dt: float, stage):
        self._update_move(dt, stage)
        self._update_lift(dt, stage)

    def _finish_turn(self, stage):
        """회전 완료 — 일반 모드는 각도 도달 시, 트윈은 실물 cmd_ack 도착 시."""
        self.motors.stop()
        self.heading = self.heading_target  # 정확한 값으로 스냅
        self.state   = "IDLE"
        self._sync_prim(stage)
        turn_cmd = self._current_turn_cmd
        self._current_turn_cmd = None
        print(f"[AGV {self.rid}] Turn done ({turn_cmd})")
        if turn_cmd:
            self._ack(turn_cmd)     # 트윈에선 침묵 (실물이 이미 보고했다)

    def _update_move(self, dt: float, stage):
        if self.state == "TURNING":
            # 트윈: 회전을 거의 끝내고 실물 cmd_ack 대기 (먼저 끝내지 않는다)
            if self._twin_holding_turn:
                return

            # 수정 68 — 실물 yaw가 살아있으면 **측정이 추정을 대체한다.**
            # 회전 중엔 발밑 마커가 계속 보이므로 시간 보간할 이유가 없다.
            pose_driven = self._apply_pose_heading()

            diff = _normalize_angle(self.heading_target - self.heading)
            # 트윈은 남은 각도가 총각도의 1% 이내면 홀드 (일반은 ANGLE_TOLERANCE에서 완료)
            limit = ANGLE_TOLERANCE
            if TWIN_MODE and self._twin_turn_total > 0:
                limit = max(ANGLE_TOLERANCE,
                            self._twin_turn_total * (1.0 - TWIN_HOLD_RATIO))

            if abs(diff) < limit:
                self.motors.stop()
                if TWIN_MODE:
                    self._twin_holding_turn = True   # 완료는 실물 ack가 시킨다
                    self._sync_prim(stage)
                    return
                self._finish_turn(stage)
            else:
                if TWIN_MODE:
                    speed = self._turn_wheel_speed   # 실측 회전시간에 맞춘 등속
                else:
                    speed = min(1.0, abs(diff) / 0.5) * TURN_SPEED  # 오버슈트 방지 감속
                if diff > 0:
                    self.motors.set_speeds(-speed,  speed)  # 반시계
                else:
                    self.motors.set_speeds( speed, -speed)  # 시계
                if not pose_driven:
                    # pose가 없을 때만 시간 적분으로 heading을 만든다(추정).
                    # pose가 있으면 heading은 이미 실측으로 정해졌다 — 여기서 또 더하면 이중 적용.
                    _, omega = self.motors.get_velocity()
                    self.heading = _normalize_angle(self.heading + omega * dt)
                self._sync_prim(stage)

        elif self.state == "MOVING":
            # 트윈: 홀드 지점 도달 → 실물 마커가 올 때까지 정지 (sync_to_node가 풀어준다)
            if self._twin_holding:
                return

            # 트윈은 엣지 끝이 아니라 홀드 지점(92%)을 목표로 달린다
            goal = (self._twin_hold_pos
                    if (TWIN_MODE and self._twin_hold_pos is not None)
                    else self.target_pos)
            diff = goal - self.pos
            dist = float(np.linalg.norm(diff))

            if dist < POSITION_TOLERANCE:
                self.motors.stop()
                self.pos = goal.copy()
                self._sync_prim(stage)

                if TWIN_MODE and self._twin_hold_pos is not None:
                    if not self._twin_holding:
                        self._twin_holding = True
                        self._twin_hold_t = time.time()   # 대기 시작 (= 추정이 빨랐던 만큼)
                    return              # 노드 안착은 실물 마커가 왔을 때만

                self.current_node = self._moving_to_node
                self.state        = "IDLE"
                print(f"[AGV {self.rid}] Reached node {self.current_node}")
                # 다음 명령은 ArUco 마커 감지 → /agv/marker → 서버가 결정
            else:
                # 직진 + 작은 각도 보정 (Webots Navigator 와 동일 방식)
                angle_to_target = float(np.arctan2(diff[1], diff[0]))
                err = _normalize_angle(angle_to_target - self.heading)
                correction = max(-0.5, min(0.5, err * 2.0))
                # 트윈은 실측에 맞춘 속도(_move_speed), 일반 모드는 MOVE_SPEED
                speed = self._move_speed if TWIN_MODE else MOVE_SPEED
                self.motors.set_speeds(speed - correction,
                                       speed + correction)
                v, omega = self.motors.get_velocity()
                self.heading = _normalize_angle(self.heading + omega * dt)
                self.pos += v * dt * np.array([np.cos(self.heading),
                                               np.sin(self.heading)])
                # 실제 이동 거리 기반 바퀴 회전각 누적
                self.wheel_angle += abs(v) * dt / WHEEL_RADIUS
                self._sync_prim(stage)

    def _finish_lift(self, stage):
        """리프트 완료 — 일반 모드는 높이 도달 시, 트윈은 실물 cmd_ack 도착 시."""
        self.lift_z     = self.lift_target_z
        prev_state      = self.lift_state
        self.lift_state = "IDLE"
        self._sync_lift(stage)

        if prev_state == "RAISING":
            print(f"[AGV {self.rid}] Lift UP done (shelf {self.carrying_shelf})")
            # 약점 4: 실제 든 선반 보고 (None이면 빈 리프트 → 서버 감지)
            self._ack("lift_up", self.carrying_shelf)
        elif prev_state == "LOWERING":
            shelf_id = self.carrying_shelf
            self.carrying_shelf = None
            self._place_shelf(stage, shelf_id)
            print(f"[AGV {self.rid}] Lift DOWN done (shelf {shelf_id})")
            self._ack("lift_down", shelf_id)

    def _update_lift(self, dt: float, stage):
        if self.lift_state == "IDLE":
            return
        # 트윈: 리프트를 거의 끝내고 실물 cmd_ack 대기
        if self._twin_holding_lift:
            return

        diff  = self.lift_target_z - self.lift_z
        speed = self._lift_speed if TWIN_MODE else LIFT_SPEED   # 트윈은 실측에 맞춘 속도
        step  = min(speed * dt, abs(diff))

        # 트윈은 남은 높이가 총높이의 1% 이내면 홀드 (일반은 5mm에서 완료)
        limit = 0.005
        if TWIN_MODE and self._twin_lift_total > 0:
            limit = max(0.005, self._twin_lift_total * (1.0 - TWIN_HOLD_RATIO))

        if abs(diff) < limit:
            if TWIN_MODE:
                self._twin_holding_lift = True   # 완료는 실물 ack가 시킨다
                self._sync_lift(stage)
                return
            self._finish_lift(stage)
        else:
            self.lift_z += step if diff > 0 else -step
            self._sync_lift(stage)

    # ─── USD 동기화 ──────────────────────────────────────────────────────────

    def _sync_prim(self, stage):
        x, y = float(self.pos[0]), float(self.pos[1])
        h = self.heading
        cos_h, sin_h = np.cos(h), np.sin(h)

        if self._use_cad:
            # CAD 모드: AGV 루트 prim 위치/방향만 업데이트
            self._set_translate(stage, f"/World/AGV_{self.rid}", x, y, 0.0)
            self._set_orient_z(stage, f"/World/AGV_{self.rid}", h)
            if self.carrying_shelf is not None:
                self._sync_shelf(stage)
            return

        # 기본 도형 모드
        self._set_translate(stage, f"/World/AGV_{self.rid}_body", x, y, self.BODY_Z)
        self._set_orient_z(stage, f"/World/AGV_{self.rid}_body", h)

        # LED
        self._set_translate(stage, f"/World/AGV_{self.rid}_led", x, y, 0.155)

        # 바퀴: 위치 + 롤링 + heading 방향
        q_wheel = self._wheel_quat(self.wheel_angle, self.heading)
        for i, (wdx, wdy) in enumerate(self.WHEEL_OFFSETS):
            rx = wdx * cos_h - wdy * sin_h
            ry = wdx * sin_h + wdy * cos_h
            self._set_translate(stage, f"/World/AGV_{self.rid}_wheel_{i}",
                                x + rx, y + ry, self.WHEEL_Z)
            self._set_orient_q(stage, f"/World/AGV_{self.rid}_wheel_{i}", q_wheel)

        # 시저리프트: lift_z에 따라 레그 중심 Z + 기울기 동적 계산
        sc_z, sc_tilt = self._scissor_state(self.lift_z)
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_a",
                            x - 0.06 * sin_h, y + 0.06 * cos_h, sc_z)
        self._set_orient_q(stage, f"/World/AGV_{self.rid}_scissor_a",
                           self._scissor_quat(h, sc_tilt, +1))
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_b",
                            x + 0.06 * sin_h, y - 0.06 * cos_h, sc_z)
        self._set_orient_q(stage, f"/World/AGV_{self.rid}_scissor_b",
                           self._scissor_quat(h, sc_tilt, -1))
        self._set_translate(stage, f"/World/AGV_{self.rid}_lift_plate",
                            x, y, self.lift_z)

        if self.carrying_shelf is not None:
            self._sync_shelf(stage)

    def _heading_quat(self, heading: float) -> Gf.Quatf:
        h = heading / 2.0
        return Gf.Quatf(float(np.cos(h)), Gf.Vec3f(0.0, 0.0, float(np.sin(h))))

    def _read_shelf_orient(self, shelf_id) -> Gf.Quatf:
        """선반의 현재 OrientOp 읽기 (없으면 identity 반환)"""
        try:
            prim = omni.usd.get_context().get_stage().GetPrimAtPath(f"/World/Shelf_{shelf_id}")
            if prim.IsValid():
                for op in UsdGeom.Xformable(prim).GetOrderedXformOps():
                    if op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                        v = op.Get()
                        if v:
                            return Gf.Quatf(float(v.GetReal()),
                                            Gf.Vec3f(float(v.GetImaginary()[0]),
                                                     float(v.GetImaginary()[1]),
                                                     float(v.GetImaginary()[2])))
        except Exception:
            pass
        return Gf.Quatf(1.0, Gf.Vec3f(0.0, 0.0, 0.0))

    def _sync_shelf(self, stage):
        """선반 위치/방향 동기화 — AGV 회전에 따라 선반도 함께 회전 (픽업 순간 스냅 없음)"""
        if self.carrying_shelf is None:
            return
        x, y = float(self.pos[0]), float(self.pos[1])
        dz = max(0.0, self.lift_z - self.LIFT_CONTACT_Z)

        shelf_root = f"/World/Shelf_{self.carrying_shelf}"
        prim = stage.GetPrimAtPath(shelf_root)
        if not prim.IsValid():
            return

        orient = self._heading_quat(self.heading) * self.shelf_offset

        xform = UsdGeom.Xformable(prim)
        has_t, has_o = False, False
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(x, y, dz))
                has_t = True
            elif op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                op.Set(orient)
                has_o = True
        if not has_t:
            xform.AddTranslateOp().Set(Gf.Vec3d(x, y, dz))
        if not has_o:
            xform.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(orient)

    def _place_shelf(self, stage, shelf_id: int):
        """선반 내려놓기 완료 — AGV 현재 위치에 놓기 (heading 리셋)"""
        shelf_root = f"/World/Shelf_{shelf_id}"
        prim = stage.GetPrimAtPath(shelf_root)
        if not prim.IsValid():
            return
        x, y = float(self.pos[0]), float(self.pos[1])
        # 내려놓은 위치를 origin으로 갱신 (다음 pickup 시 참조됨)
        shelf_origins[shelf_id] = (x, y)
        xform = UsdGeom.Xformable(prim)
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(x, y, 0.0))

    @staticmethod
    def _set_quat_op(op, w: float, x: float, y: float, z: float):
        """orient op에 쿼터니언 설정 — Quatf/Quatd 타입 자동 대응"""
        try:
            op.Set(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
        except Exception:
            op.Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)))

    def _sync_lift(self, stage):
        x, y = float(self.pos[0]), float(self.pos[1])
        h = self.heading
        sin_h, cos_h = np.sin(h), np.cos(h)

        self._set_translate(stage, f"/World/AGV_{self.rid}_lift_plate",
                            x, y, self.lift_z)

        # 시저리프트 레그도 상판과 함께 이동
        sc_z, sc_tilt = self._scissor_state(self.lift_z)
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_a",
                            x - 0.06 * sin_h, y + 0.06 * cos_h, sc_z)
        self._set_orient_q(stage, f"/World/AGV_{self.rid}_scissor_a",
                           self._scissor_quat(h, sc_tilt, +1))
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_b",
                            x + 0.06 * sin_h, y - 0.06 * cos_h, sc_z)
        self._set_orient_q(stage, f"/World/AGV_{self.rid}_scissor_b",
                           self._scissor_quat(h, sc_tilt, -1))

        if self.carrying_shelf is not None:
            self._sync_shelf(stage)

    # ─── USD 헬퍼 ────────────────────────────────────────────────────────────

    @staticmethod
    def _wheel_quat(wheel_angle: float, heading: float) -> Gf.Quatd:
        """바퀴 쿼터니언: 90°X 기본자세 + Y축 롤링 + Z축 heading

        AGV 회전 시 바퀴도 heading 방향을 바라보게 함.
        q_head * q_roll * q_base: base → 롤링 → heading 정렬
        """
        q_base = Gf.Quatd(0.7071068, Gf.Vec3d(0.7071068, 0.0, 0.0))
        ha = wheel_angle / 2.0
        q_roll = Gf.Quatd(float(np.cos(ha)), Gf.Vec3d(0.0, float(np.sin(ha)), 0.0))
        hh = heading / 2.0
        q_head = Gf.Quatd(float(np.cos(hh)), Gf.Vec3d(0.0, 0.0, float(np.sin(hh))))
        return q_head * q_roll * q_base

    @staticmethod
    def _scissor_state(lift_z: float):
        """lift_z 에서 시저 레그 중심 Z와 기울기 각도 반환 (정확한 기하학)

        body_top = BODY_Z(0.10) + scale_z_half(0.04) = 0.14
        lift_bot = lift_z - plate_half(0.01)
        arm_half = scale_z(0.28) / 2 = 0.14

        center_z = (body_top + lift_bot) / 2
        tilt     = arccos(vertical_half / arm_half)
        """
        body_top = 0.14
        lift_bot = lift_z - 0.01
        center_z = (body_top + lift_bot) / 2.0
        arm_half = 0.14
        half_h   = max(0.0, min(arm_half, (lift_bot - body_top) / 2.0))
        tilt     = float(np.arccos(half_h / arm_half))
        return center_z, tilt

    @staticmethod
    def _scissor_quat(heading: float, tilt: float, sign: int) -> Gf.Quatd:
        """시저 레그 쿼터니언: heading 방향 기준 tilt 각도 (sign=+1 또는 -1)"""
        hh = heading / 2.0
        q_head = Gf.Quatd(float(np.cos(hh)), Gf.Vec3d(0.0, 0.0, float(np.sin(hh))))
        ta = sign * tilt / 2.0
        q_tilt = Gf.Quatd(float(np.cos(ta)), Gf.Vec3d(0.0, float(np.sin(ta)), 0.0))
        return q_head * q_tilt

    @staticmethod
    def _set_translate(stage, path: str, x: float, y: float, z: float):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid():
            return
        attr = prim.GetAttribute("xformOp:translate")
        if attr:
            attr.Set(Gf.Vec3d(x, y, z))

    @staticmethod
    def _set_orient_z(stage, path: str, angle_rad: float):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid():
            return
        half = angle_rad / 2.0
        q = Gf.Quatd(float(np.cos(half)), Gf.Vec3d(0.0, 0.0, float(np.sin(half))))
        attr = prim.GetAttribute("xformOp:orient")
        if attr and attr.IsValid():
            attr.Set(q)
        else:
            UsdGeom.Xformable(prim).AddOrientOp().Set(q)

    @staticmethod
    def _set_orient_q(stage, path: str, q: Gf.Quatd):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid():
            return
        attr = prim.GetAttribute("xformOp:orient")
        if attr and attr.IsValid():
            try:
                attr.Set(q)
            except Exception:
                imag = q.GetImaginary()
                qf = Gf.Quatf(float(q.GetReal()),
                              Gf.Vec3f(float(imag[0]), float(imag[1]), float(imag[2])))
                attr.Set(qf)
        else:
            UsdGeom.Xformable(prim).AddOrientOp().Set(q)



# ═══════════════════════════════════════════════════════════════════════════════
# 색상 팔레트
# ═══════════════════════════════════════════════════════════════════════════════

C_FRAME       = np.array([0.40, 0.40, 0.44])
C_SHELF_BOARD = np.array([0.45, 0.28, 0.10])
C_WS          = np.array([0.08, 0.68, 0.08])
C_AGV1        = np.array([0.95, 0.32, 0.08])
C_AGV2        = np.array([0.10, 0.38, 0.90])
C_WHEEL       = np.array([0.10, 0.10, 0.10])
C_SCISSOR     = np.array([0.85, 0.68, 0.05])
C_LED         = {1: np.array([1.0,  0.50, 0.0]),
                 2: np.array([0.0,  0.60, 1.0])}

# 박스 색상 팔레트
C_BOX = [
    np.array([0.72, 0.52, 0.04]),  # 갈색 택배
    np.array([0.20, 0.45, 0.72]),  # 파란 박스
    np.array([0.85, 0.15, 0.10]),  # 빨간 박스
    np.array([0.15, 0.65, 0.15]),  # 초록 박스
    np.array([0.70, 0.70, 0.70]),  # 회색 박스
]
C_TABLE_TOP   = np.array([0.55, 0.38, 0.18])  # 나무색 상판
C_TABLE_LEG   = np.array([0.58, 0.60, 0.63])  # 금속 다리
C_WORKER_SUIT = np.array([0.98, 0.80, 0.10])  # 노란 작업복
C_WORKER_HEAD = np.array([0.92, 0.72, 0.56])  # 살색 머리
C_WALL        = np.array([0.55, 0.55, 0.52])  # 창고 벽 (콘크리트 회색)
C_TRAY        = np.array([0.35, 0.38, 0.42])  # 금속 트레이

WHEEL_QUAT     = np.array([0.7071, 0.7071, 0.0,    0.0])
# lift_z=0.25(하강) 기준: tilt=arccos(0.05/0.14)≈69° → ta≈34.5°
SCISSOR_QUAT_A = np.array([0.8225, 0.0,    0.5688,  0.0])
SCISSOR_QUAT_B = np.array([0.8225, 0.0,   -0.5688,  0.0])

LEG_SIZE    = 0.05
LEG_HEIGHT  = 0.85
LEG_OFFSET  = 0.35
BOARD_WIDTH = 0.76
BOARD_THK   = 0.025
BOARD_ZS    = [0.40, 0.62, 0.84]
LEG_CORNERS = [(-1, -1), (1, -1), (-1, 1), (1, 1)]

MARKER_SIZE = 0.15
MARKER_Z    = 0.002


shelf_origins: dict = {}


# ═══════════════════════════════════════════════════════════════════════════════
# 씬 구성 헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _load_usd(stage, prim_path: str, usd_path: str):
    """USD 파일을 스테이지에 로드 — CAD 파일 교체 시 사용되는 함수"""
    from isaacsim.core.utils.stage import add_reference_to_stage
    add_reference_to_stage(usd_path=usd_path, prim_path=prim_path)
    print(f"  [CAD] 로드: {usd_path} -> {prim_path}")


def build_shelf(stage, node_id: int, x: float, y: float):
    """선반 빌드 — CAD_PATHS['shelf'] 지정 시 USD 로드, 없으면 기본 도형"""
    root = f"/World/Shelf_{node_id}"
    UsdGeom.Xform.Define(stage, root)
    shelf_origins[node_id] = (x, y)

    if CAD_PATHS.get("shelf"):
        _load_usd(stage, f"{root}/mesh", CAD_PATHS["shelf"])
        return

    # 기본 도형 — 자식 prim 좌표는 루트 기준 로컬 좌표 (회전 시 올바르게 동작)
    for i, (dx, dy) in enumerate(LEG_CORNERS):
        VisualCuboid(
            prim_path=f"{root}/col_{i}", name=f"shelf_{node_id}_col_{i}",
            position=np.array([dx * LEG_OFFSET, dy * LEG_OFFSET, LEG_HEIGHT / 2]),
            scale=np.array([LEG_SIZE, LEG_SIZE, LEG_HEIGHT]), color=C_FRAME,
        )
    for i, bz in enumerate(BOARD_ZS):
        beam_z   = bz - 0.032
        beam_len = BOARD_WIDTH - LEG_SIZE * 2
        beam_thk = 0.03
        VisualCuboid(
            prim_path=f"{root}/board_{i}", name=f"shelf_{node_id}_board_{i}",
            position=np.array([0.0, 0.0, bz]),
            scale=np.array([BOARD_WIDTH, BOARD_WIDTH, BOARD_THK]), color=C_SHELF_BOARD,
        )
        for name, px, py, sx, sy in [
            ("beam_front",  0.0,        -LEG_OFFSET, beam_len, beam_thk),
            ("beam_back",   0.0,         LEG_OFFSET, beam_len, beam_thk),
            ("beam_left",  -LEG_OFFSET,  0.0,        beam_thk, beam_len),
            ("beam_right",  LEG_OFFSET,  0.0,        beam_thk, beam_len),
        ]:
            VisualCuboid(
                prim_path=f"{root}/{name}_{i}", name=f"shelf_{node_id}_{name}_{i}",
                position=np.array([px, py, beam_z]),
                scale=np.array([sx, sy, beam_thk]), color=C_FRAME,
            )

    # 선반 층마다 트레이 + 박스
    TRAY_W  = 0.58   # 트레이 가로
    TRAY_D  = 0.58   # 트레이 세로
    TRAY_BH = 0.012  # 트레이 바닥 두께
    TRAY_WH = 0.065  # 트레이 벽 높이
    TRAY_WT = 0.025  # 트레이 벽 두께

    for lvl, bz in enumerate(BOARD_ZS):
        base_z = bz + BOARD_THK / 2  # 선반 판 윗면

        # 트레이 바닥
        VisualCuboid(
            prim_path=f"{root}/tray_bot_{lvl}",
            name=f"shelf_{node_id}_tray_bot_{lvl}",
            position=np.array([0.0, 0.0, base_z + TRAY_BH / 2]),
            scale=np.array([TRAY_W, TRAY_D, TRAY_BH]), color=C_TRAY,
        )
        # 트레이 벽 4개 (앞/뒤/좌/우)
        inner_d = TRAY_D - TRAY_WT * 2
        for wname, px, py, sx, sy in [
            ("wf", 0.0,                       -(TRAY_D / 2 - TRAY_WT / 2), TRAY_W,  TRAY_WT),
            ("wb", 0.0,                        (TRAY_D / 2 - TRAY_WT / 2), TRAY_W,  TRAY_WT),
            ("wl", -(TRAY_W / 2 - TRAY_WT / 2), 0.0,                      TRAY_WT, inner_d),
            ("wr",  (TRAY_W / 2 - TRAY_WT / 2), 0.0,                      TRAY_WT, inner_d),
        ]:
            VisualCuboid(
                prim_path=f"{root}/tray_{wname}_{lvl}",
                name=f"shelf_{node_id}_tray_{wname}_{lvl}",
                position=np.array([px, py, base_z + TRAY_BH + TRAY_WH / 2]),
                scale=np.array([sx, sy, TRAY_WH]), color=C_TRAY,
            )
        # 소품들 — 트레이 안에 결정론적 랜덤 배치 (node_id + 층 기반 시드)
        rng      = np.random.RandomState(node_id * 31 + lvl * 7)
        n_items  = int(rng.randint(4, 7))          # 4~6개
        inner_r  = TRAY_W / 2 - TRAY_WT - 0.07    # 배치 가능 반경 (벽 안쪽)
        for ii in range(n_items):
            iw    = float(rng.uniform(0.07, 0.13))
            id_   = float(rng.uniform(0.06, 0.12))
            ih    = float(rng.uniform(0.07, 0.16))
            ix    = float(rng.uniform(-inner_r, inner_r))
            iy    = float(rng.uniform(-inner_r, inner_r))
            color = C_BOX[int(rng.randint(0, len(C_BOX)))]
            VisualCuboid(
                prim_path=f"{root}/item_{lvl}_{ii}",
                name=f"shelf_{node_id}_item_{lvl}_{ii}",
                position=np.array([ix, iy, base_z + TRAY_BH + ih / 2]),
                scale=np.array([iw, id_, ih]), color=color,
            )

    # 선반 전체를 감싸는 보이지 않는 collision box (물리 충돌 전용)
    col_prim = UsdGeom.Cube.Define(stage, f"{root}/collision_box").GetPrim()
    col_xform = UsdGeom.Xformable(col_prim)
    col_xform.AddScaleOp().Set(Gf.Vec3f(0.78, 0.78, LEG_HEIGHT))
    col_xform.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, LEG_HEIGHT / 2.0))
    UsdPhysics.CollisionAPI.Apply(col_prim)
    col_prim.GetAttribute("visibility").Set("invisible")


def build_workstation(stage, node_id: int, x: float, y: float):
    """작업대 빌드 — WS 노드 왼쪽에 나란히 배치 (같은 Y 레벨)

    배치:
      (x,    y)    선반/AGV 도착 (WS 노드)
      (x-0.3, y)   작업자
      (x-0.7, y)   컨베이어 중심 (X방향으로 흐름)
      (x-1.7, y)   수납 박스 (-X 끝)
    """
    root = f"/World/WS_{node_id}"

    if CAD_PATHS.get("workstation"):
        UsdGeom.Xform.Define(stage, root)
        _load_usd(stage, f"{root}/mesh", CAD_PATHS["workstation"])
        return

    UsdGeom.Xform.Define(stage, root)

    CONV_H   = 0.80   # 컨베이어 높이
    CONV_LX  = 1.30   # X방향 길이 (벨트 흐름 방향)
    CONV_W   = 0.42   # Y방향 폭
    CONV_CX  = x - 1.10   # 컨베이어 중심 X (선반 폭 0.76 고려해 충분히 이격)
    CONV_CY  = y          # 컨베이어 중심 Y (WS 노드와 동일 Y)

    # 프레임 — Y방향 양쪽 빔 (X 방향으로 긴 사각 프레임)
    for si, sy_off in enumerate([-1, 1]):
        VisualCuboid(
            prim_path=f"{root}/side_{si}", name=f"ws_{node_id}_side_{si}",
            position=np.array([CONV_CX, CONV_CY + sy_off * CONV_W / 2, CONV_H]),
            scale=np.array([CONV_LX, 0.04, 0.07]), color=C_TABLE_LEG,
        )
    # 다리 4개
    for li, (ldx, ldy) in enumerate([(-1, -1), (1, -1), (-1, 1), (1, 1)]):
        VisualCylinder(
            prim_path=f"{root}/leg_{li}", name=f"ws_{node_id}_leg_{li}",
            position=np.array([CONV_CX + ldx * (CONV_LX / 2 - 0.06),
                               CONV_CY + ldy * (CONV_W  / 2 - 0.04),
                               (CONV_H - 0.03) / 2]),
            radius=0.025, height=CONV_H - 0.03, color=C_TABLE_LEG,
        )
    # 롤러 — Y방향으로 눕힌 실린더, X방향으로 8개 나열
    # cylinder 기본축=Z, 90°X 회전 → 축=Y: q=(cos45, sin45, 0, 0)=(0.7071, 0.7071, 0, 0)
    ROLLER_Q = np.array([0.7071, 0.7071, 0.0, 0.0])
    for ri in range(8):
        rx = CONV_CX - CONV_LX / 2 + 0.08 + ri * (CONV_LX - 0.16) / 7
        VisualCylinder(
            prim_path=f"{root}/roller_{ri}", name=f"ws_{node_id}_roller_{ri}",
            position=np.array([rx, CONV_CY, CONV_H + 0.022]),
            orientation=ROLLER_Q,
            radius=0.022, height=CONV_W - 0.06,
            color=np.array([0.75, 0.75, 0.78]),
        )
    # 수납 박스 — 컨베이어 끝(-X)에 배치 (물건이 흘러 쌓이는 곳)
    bin_x = CONV_CX - CONV_LX / 2 - 0.28
    VisualCuboid(
        prim_path=f"{root}/out_bin", name=f"ws_{node_id}_out_bin",
        position=np.array([bin_x, CONV_CY, 0.25]),
        scale=np.array([0.44, 0.58, 0.46]), color=np.array([0.30, 0.30, 0.33]),
    )
    # 수납 박스 안 물건 (쌓인 느낌)
    VisualCuboid(
        prim_path=f"{root}/out_items", name=f"ws_{node_id}_out_items",
        position=np.array([bin_x, CONV_CY, 0.50]),
        scale=np.array([0.38, 0.50, 0.06]), color=np.array([0.85, 0.75, 0.20]),
    )
    # 표시 LED (컨베이어 선반 쪽 끝)
    VisualCylinder(
        prim_path=f"{root}/status_led", name=f"ws_{node_id}_led",
        position=np.array([CONV_CX + CONV_LX / 2 - 0.1, CONV_CY + CONV_W / 2, CONV_H + 0.10]),
        radius=0.022, height=0.05, color=np.array([0.1, 0.9, 0.1]),
    )

    # ── 터치스크린 모니터 ──────────────────────────────────────────────────────
    # 컨베이어 +Y 끝(카메라 반대쪽)에 폴 마운트, 화면은 -Y(카메라/작업자 쪽)을 향함
    sx      = CONV_CX + 0.20              # 컨베이어 중앙 약간 +X
    pole_y  = CONV_CY + CONV_W / 2 + 0.18  # 컨베이어 +Y 바깥
    mon_y   = CONV_CY + CONV_W / 2 + 0.04  # 모니터 Y (컨베이어 가장자리 바로 위)

    # 폴
    VisualCylinder(
        prim_path=f"{root}/scr_pole", name=f"ws_{node_id}_scr_pole",
        position=np.array([sx, pole_y, 0.60]),
        radius=0.018, height=1.22, color=np.array([0.30, 0.30, 0.33]),
    )
    # 수평 암 (폴 → 모니터 후면, -Y 방향)
    arm_len = pole_y - mon_y
    VisualCuboid(
        prim_path=f"{root}/scr_arm", name=f"ws_{node_id}_scr_arm",
        position=np.array([sx, (pole_y + mon_y) / 2, 1.14]),
        scale=np.array([0.03, arm_len, 0.02]), color=np.array([0.30, 0.30, 0.33]),
    )
    # 모니터 베젤 (검정 테두리, 화면 앞면이 -Y 방향)
    VisualCuboid(
        prim_path=f"{root}/scr_bezel", name=f"ws_{node_id}_scr_bezel",
        position=np.array([sx, mon_y, 1.22]),
        scale=np.array([0.34, 0.038, 0.24]), color=np.array([0.07, 0.07, 0.09]),
    )
    # 스크린 면 (베젤 -Y 앞쪽 → 카메라/작업자 방향으로 노출)
    VisualCuboid(
        prim_path=f"{root}/scr_face", name=f"ws_{node_id}_scr_face",
        position=np.array([sx, mon_y - 0.022, 1.22]),
        scale=np.array([0.30, 0.005, 0.20]), color=np.array([0.10, 0.45, 0.85]),
    )
    # UI — 상단 헤더 바 (밝은 파랑)
    VisualCuboid(
        prim_path=f"{root}/scr_header", name=f"ws_{node_id}_scr_header",
        position=np.array([sx, mon_y - 0.026, 1.305]),
        scale=np.array([0.28, 0.004, 0.030]), color=np.array([0.20, 0.68, 1.00]),
    )
    # UI — 아이템 행 3개 (흰 블록)
    for i, rz in enumerate([1.25, 1.21, 1.17]):
        VisualCuboid(
            prim_path=f"{root}/scr_row_{i}", name=f"ws_{node_id}_scr_row_{i}",
            position=np.array([sx, mon_y - 0.026, rz]),
            scale=np.array([0.24, 0.003, 0.022]), color=np.array([0.92, 0.95, 1.00]),
        )
    # UI — 하단 확인 버튼 (녹색)
    VisualCuboid(
        prim_path=f"{root}/scr_btn", name=f"ws_{node_id}_scr_btn",
        position=np.array([sx, mon_y - 0.026, 1.135]),
        scale=np.array([0.10, 0.004, 0.028]), color=np.array([0.10, 0.80, 0.30]),
    )


def set_agv_visible(stage, rid: int, visible: bool):
    """AGV의 모든 파츠(바디·바퀴·시저리프트·LED…)를 통째로 보이기/숨기기.

    수정 73: 트윈 모드에서 **연결되지 않은 AGV는 그리지 않는다.**
    실물이 1대만 붙어 있는데 화면에 2대가 서 있으면 그건 거짓말이다.
    (트윈의 값어치는 '예쁨'이 아니라 '정직함'이다 — 수정 65와 같은 원칙)
    """
    prefix = f"AGV_{rid}"
    for prim in stage.Traverse():
        name = prim.GetName()
        if name == prefix or name.startswith(prefix + "_"):
            img = UsdGeom.Imageable(prim)
            if img:
                img.MakeVisible() if visible else img.MakeInvisible()


def build_agv(stage, rid: int, x: float, y: float) -> bool:
    """AGV 빌드 — CAD_PATHS['agv'] 지정 시 USD 로드, 없으면 기본 도형

    Returns:
        True  = 기본 도형 생성됨 (개별 prim sync 필요)
        False = CAD 로드됨 (루트 prim sync 만 필요)
    """
    c_agv = {1: C_AGV1, 2: C_AGV2}.get(rid, C_AGV1)

    if CAD_PATHS.get("agv"):
        root = f"/World/AGV_{rid}"
        UsdGeom.Xform.Define(stage, root)
        _load_usd(stage, f"{root}/mesh", CAD_PATHS["agv"])
        return False  # CAD 모드

    # 기본 도형 — AGV 바디: DynamicCuboid (물리 충돌 감지)
    DynamicCuboid(
        prim_path=f"/World/AGV_{rid}_body", name=f"agv_{rid}_body",
        position=np.array([x, y, IsaacAGV.BODY_Z]),
        scale=np.array([0.38, 0.38, 0.08]), color=c_agv,
        mass=50.0,
    )
    for i, (dx, dy) in enumerate(IsaacAGV.WHEEL_OFFSETS):
        VisualCylinder(
            prim_path=f"/World/AGV_{rid}_wheel_{i}", name=f"agv_{rid}_wheel_{i}",
            position=np.array([x + dx, y + dy, IsaacAGV.WHEEL_Z]),
            orientation=WHEEL_QUAT, radius=0.06, height=0.05, color=C_WHEEL,
        )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_a", name=f"agv_{rid}_scissor_a",
        position=np.array([x, y + 0.06, IsaacAGV.SCISSOR_Z]),
        orientation=SCISSOR_QUAT_A, scale=np.array([0.025, 0.04, 0.28]), color=C_SCISSOR,
    )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_b", name=f"agv_{rid}_scissor_b",
        position=np.array([x, y - 0.06, IsaacAGV.SCISSOR_Z]),
        orientation=SCISSOR_QUAT_B, scale=np.array([0.025, 0.04, 0.28]), color=C_SCISSOR,
    )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_lift_plate", name=f"agv_{rid}_lift_plate",
        position=np.array([x, y, IsaacAGV.LIFT_PLATE_Z]),
        scale=np.array([0.36, 0.36, 0.02]), color=C_SCISSOR,
    )
    VisualCylinder(
        prim_path=f"/World/AGV_{rid}_led", name=f"agv_{rid}_led",
        position=np.array([x, y, 0.155]),
        radius=0.025, height=0.03, color=C_LED.get(rid, c_agv),
    )
    return True  # 기본 도형 모드


def build_inbound_station(stage, node_id: int, x: float, y: float):
    """입고 스테이션 빌드 — 노드 동쪽(+X, 창고 밖)에 공급대 + 작업자.

    출고 작업대(build_workstation)는 -X 쪽에 컨베이어가 붙는 반대 배치라 재사용 안 함.
    AGV는 이 노드에 선반을 대고 heading 90°(동쪽)로 멈춘다(shelf_config pick_heading)
    → 선반 면이 창고 밖 작업자를 향함.

    배치:
      (x,       y)   AGV/선반 도착 (입고 노드)
      (x+0.95,  y)   공급대 (입고할 물건이 놓인 테이블)
      (x+1.75,  y)   작업자 (창고 밖 동쪽에 서서 선반에 채워 넣음)
    """
    root = f"/World/IN_{node_id}"
    UsdGeom.Xform.Define(stage, root)

    TBL_H  = 0.80
    TBL_CX = x + 0.95

    # 공급대 상판 + 다리 4개
    VisualCuboid(
        prim_path=f"{root}/table_top", name=f"in_{node_id}_table_top",
        position=np.array([TBL_CX, y, TBL_H]),
        scale=np.array([0.70, 1.00, 0.06]), color=C_TABLE_LEG,
    )
    for li, (ldx, ldy) in enumerate([(-1, -1), (1, -1), (-1, 1), (1, 1)]):
        VisualCylinder(
            prim_path=f"{root}/table_leg_{li}", name=f"in_{node_id}_table_leg_{li}",
            position=np.array([TBL_CX + ldx * 0.29, y + ldy * 0.44, (TBL_H - 0.03) / 2]),
            radius=0.025, height=TBL_H - 0.03, color=C_TABLE_LEG,
        )
    # 입고할 물건 상자 3개 (공급대 위)
    for bi, (by_off, bcol) in enumerate([
        (-0.32, np.array([0.85, 0.55, 0.15])),
        ( 0.00, np.array([0.80, 0.30, 0.25])),
        ( 0.32, np.array([0.30, 0.60, 0.80])),
    ]):
        VisualCuboid(
            prim_path=f"{root}/box_{bi}", name=f"in_{node_id}_box_{bi}",
            position=np.array([TBL_CX, y + by_off, TBL_H + 0.10]),
            scale=np.array([0.26, 0.24, 0.16]), color=bcol,
        )

    # 작업자 (창고 밖 동쪽) — 몸통 + 머리
    VisualCylinder(
        prim_path=f"{root}/worker_body", name=f"in_{node_id}_worker_body",
        position=np.array([x + 1.75, y, 0.45]),
        radius=0.16, height=0.90, color=np.array([0.20, 0.35, 0.55]),
    )
    VisualSphere(
        prim_path=f"{root}/worker_head", name=f"in_{node_id}_worker_head",
        position=np.array([x + 1.75, y, 1.02]),
        radius=0.12, color=np.array([0.90, 0.75, 0.62]),
    )
    # 입고 표시등 (초록) — 공급대 모서리
    VisualCylinder(
        prim_path=f"{root}/in_led", name=f"in_{node_id}_led",
        position=np.array([TBL_CX - 0.30, y + 0.44, TBL_H + 0.12]),
        radius=0.022, height=0.05, color=C_WS,
    )


def build_warehouse_env(stage):
    pass


def make_aruco_material(stage, node_id: int) -> UsdShade.Material:
    mat_path = f"/World/ArUcoMats/mat_{node_id}"
    material  = UsdShade.Material.Define(stage, mat_path)
    shader    = UsdShade.Shader.Define(stage, f"{mat_path}/shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(1.0)
    shader.CreateInput("metallic",  Sdf.ValueTypeNames.Float).Set(0.0)
    tex = UsdShade.Shader.Define(stage, f"{mat_path}/tex")
    tex.CreateIdAttr("UsdUVTexture")
    png_path = os.path.join(ARUCO_DIR, f"aruco_{node_id}.png")
    tex.CreateInput("file",  Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(png_path))
    tex.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("clamp")
    tex.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("clamp")
    st_reader = UsdShade.Shader.Define(stage, f"{mat_path}/stReader")
    st_reader.CreateIdAttr("UsdPrimvarReader_float2")
    st_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
    tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
        st_reader.ConnectableAPI(), "result", UsdShade.AttributeType.Output)
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        tex.ConnectableAPI(), "rgb", UsdShade.AttributeType.Output)
    material.CreateSurfaceOutput().ConnectToSource(
        shader.ConnectableAPI(), "surface", UsdShade.AttributeType.Output)
    return material


def create_aruco_marker(stage, node_id: int, x: float, y: float):
    prim_path = f"/World/Marker_{node_id}"
    h = MARKER_SIZE / 2
    mesh = UsdGeom.Mesh.Define(stage, prim_path)
    mesh.CreatePointsAttr(Vt.Vec3fArray([
        Gf.Vec3f(x - h, y - h, MARKER_Z), Gf.Vec3f(x + h, y - h, MARKER_Z),
        Gf.Vec3f(x + h, y + h, MARKER_Z), Gf.Vec3f(x - h, y + h, MARKER_Z),
    ]))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([4]))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray([0, 1, 2, 3]))
    mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * 4))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    st_primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying)
    st_primvar.Set(Vt.Vec2fArray([
        Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(1, 1), Gf.Vec2f(0, 1),
    ]))
    material = make_aruco_material(stage, node_id)
    UsdShade.MaterialBindingAPI(mesh.GetPrim()).Bind(material)


# ═══════════════════════════════════════════════════════════════════════════════
# 메인 — 씬 구성
# ═══════════════════════════════════════════════════════════════════════════════

world = World(stage_units_in_meters=1.0)
stage = omni.usd.get_context().get_stage()

# 흰색 배경 — DomeLight (RTX Real-Time + Path Tracing 모두 동작)
_dome = UsdLux.DomeLight.Define(stage, "/World/DomeLight")
_dome.CreateIntensityAttr(1500.0)
_dome.CreateColorAttr(Gf.Vec3f(1.0, 1.0, 1.0))
_dome.CreateTextureFileAttr("")

# 그리드(x=0~8, y=-0.5~4.5) + 작업대/작업자 영역(x≈-2.5) + 입고 스테이션(x≈9.3) 포함 바닥
# x: -2.5 ~ 9.5 → 중심 3.5, 폭 12.0 / y: -1.0 ~ 5.0 → 중심 2.0, 깊이 6.0
VisualCuboid(
    prim_path="/World/Floor", name="floor",
    position=np.array([3.5, 2.0, -0.01]),
    scale=np.array([12.0, 6.0, 0.02]),
    color=np.array([0.25, 0.25, 0.25]),
)

for node_id in shelf_node_ids:
    node = nodes[node_id]
    build_shelf(stage, node_id, node["x"], node["y"])

for node_id in ws_node_ids:
    node = nodes[node_id]
    build_workstation(stage, node_id, node["x"], node["y"])

# 입고 스테이션 (shelf_config.json inbound_station — 현재 노드 48, 동쪽 밖 작업자)
for node_str in shelf_cfg.get("inbound_station", {}):
    node = nodes[int(node_str)]
    build_inbound_station(stage, int(node_str), node["x"], node["y"])

build_warehouse_env(stage)

agvs: dict[int, IsaacAGV] = {}
for rid, home_node in sorted(robot_homes.items()):
    n = nodes[home_node]
    x, y = n["x"], n["y"]
    build_agv(stage, rid, x, y)
    agvs[rid] = IsaacAGV(rid, home_node)
    print(f"[AGV {rid}] Home: node {home_node}  ({x}, {y})")

    # 수정 73 — 트윈 모드에서는 실물이 붙기 전까지 그리지 않는다.
    # 실물 1대만 연결됐는데 화면에 2대가 서 있으면 트윈이 거짓을 그리는 것이다.
    # 나중에 2번째 AGV가 붙으면 그때 등장한다(아래 main loop).
    if TWIN_MODE:
        set_agv_visible(stage, rid, False)
        print(f"[AGV {rid}] (트윈) 실물 연결 대기 중 — 연결되면 화면에 등장합니다")

world.reset()

# 선반 루트 위치 설정 — world.reset() 이후에 해야 유지됨
# 자식 prim은 로컬 좌표로 생성되었으므로, 루트에 translate만 추가하면 됨
for node_id in shelf_node_ids:
    node = nodes[node_id]
    shelf_root_prim = stage.GetPrimAtPath(f"/World/Shelf_{node_id}")
    if shelf_root_prim.IsValid():
        UsdGeom.Xformable(shelf_root_prim).AddTranslateOp().Set(
            Gf.Vec3d(node["x"], node["y"], 0.0))

# AGV 초기 상태 동기화 (시저리프트/바퀴 포함)
for agv in agvs.values():
    agv._sync_prim(stage)

# ─── 물리 레이어 초기화 ─────────────────────────────────────────────────────
# AGV 바디 → kinematic (코드가 위치 직접 제어, 물리엔진은 충돌만 감지)
for agv in agvs.values():
    prim = stage.GetPrimAtPath(f"/World/AGV_{agv.rid}_body")
    if prim.IsValid():
        rb = UsdPhysics.RigidBodyAPI.Apply(prim)
        rb.CreateKinematicEnabledAttr(True)

# 선반 루트 → kinematic rigid body (자식 collision_box가 충돌 담당)
for nid in shelf_node_ids:
    prim = stage.GetPrimAtPath(f"/World/Shelf_{nid}")
    if prim.IsValid():
        rb = UsdPhysics.RigidBodyAPI.Apply(prim)
        rb.CreateKinematicEnabledAttr(True)
        UsdPhysics.MassAPI.Apply(prim).CreateMassAttr().Set(30.0)

# 바닥 → 정적 충돌체 (RigidBody 없음 = 움직이지 않는 충돌면)
floor_prim = stage.GetPrimAtPath("/World/Floor")
if floor_prim.IsValid():
    UsdPhysics.CollisionAPI.Apply(floor_prim)

print("  물리 레이어 초기화 완료")
# ────────────────────────────────────────────────────────────────────────────

print("  선반/작업대/AGV 배치 완료")

for node_id, node in nodes.items():
    create_aruco_marker(stage, node_id, node["x"], node["y"])
print(f"  ArUco 마커 배치 완료: {len(nodes)}개")

# ─── Bridge + Camera 초기화 ─────────────────────────────────────────────────
# 각 AGV에 Bridge(cmd_handler 콜백 모드) + IsaacCamera 연결
# 실물 전환 시: cmd_handler=None + open_uart() → UART 모드
#              camera = RpiCamera("camera_calibration.pkl")
all_node_ids = set(nodes.keys())
bridges: dict[int, Bridge] = {}
for agv in agvs.values():
    bridge = Bridge(
        rid=agv.rid,
        cmd_handler=agv._on_cmd_from_bridge,
        # 트윈 모드에서만 /agv/marker 구독 (실물 위치 추종). 일반 모드는 구독 안 함 —
        # 자기가 발행한 마커를 되받으면 자기 위치를 자기가 덮어쓴다.
        marker_handler=(agv._on_marker_from_real if TWIN_MODE else None),
        ack_handler=(agv._on_ack_from_real if TWIN_MODE else None),
        # 수정 68 — 실물의 연속 자세(/agv/pose) 구독 → 회전을 실시간으로 따라간다
        pose_handler=(agv._on_pose_from_real if TWIN_MODE else None),
    )
    cam = IsaacCamera(
        get_pos_fn=lambda a=agv: a.pos,
        get_heading_fn=lambda a=agv: a.heading,
        nodes=nodes,
        marker_node_ids=all_node_ids,
        detect_radius=CAM_DETECT_RADIUS,
        detect_interval=DETECT_INTERVAL,
    )
    agv.set_bridge(bridge)
    agv.set_camera(cam)
    bridge.connect()
    bridges[agv.rid] = bridge
    print(f"[AGV {agv.rid}] Bridge + IsaacCamera 연결 완료")

set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

print()
print("=" * 60)
print("  Isaac Sim 5.1.0 — Step 7: Kinematic Physics + cmd-based 제어")
print("=" * 60)
print(f"  모드: {'디지털 트윈 (실물 마커 추종 · 발행 안 함)' if TWIN_MODE else '일반 시뮬 (Isaac이 AGV 역할)'}")
if TWIN_MODE:
    print(f"  페이싱 초기추정: 1칸 {TWIN_EDGE_SECS_INIT:.1f}초 / 회전 {TWIN_TURN_SECS_INIT:.1f}초 "
          f"/ 리프트 {TWIN_LIFT_SECS_INIT:.1f}초 → 실측으로 자동 보정")
    print(f"          동작 {TWIN_HOLD_RATIO*100:.0f}% 지점에서 실물 신호 대기 "
          f"(직진=마커, 회전·리프트=cmd_ack) → 먼저 끝내지 않는다")
print(f"  AGV-1 홈: {robot_homes[1]}  AGV-2 홈: {robot_homes[2]}")
print(f"  이동 방식: TURNING({TURN_SPEED:.2f}m/s) -> MOVING({MOVE_SPEED}m/s)")
print(f"  물리: AGV 바디(kinematic) + 선반(kinematic+collision) + 바닥(static)")
print(f"  모터: IsaacMotors  카메라: IsaacCamera  브릿지: Bridge(callback)")
print(f"  [실물 전환] 모터: RaspiMotors / 카메라: RpiCamera / Bridge(uart)")
cad_active = [k for k, v in CAD_PATHS.items() if v]
print(f"  CAD: {'활성 ' + str(cad_active) if cad_active else '없음 (기본 도형)'}")
print("-" * 60)
print("  창을 닫으면 종료됩니다.  [Space]: 일시정지/재개")
print("=" * 60)
print()

for _ in range(10):
    simulation_app.update()

# ─── 키보드 일시정지 ──────────────────────────────────────────────────────────
_paused = False
_space_was_down = False
_input_iface = carb.input.acquire_input_interface()
_keyboard = omni.appwindow.get_default_app_window().get_keyboard()

# ─── 메인 루프 ────────────────────────────────────────────────────────────────
while simulation_app.is_running():
    world.step(render=True)

    # Space 폴링: 눌린 순간(엣지)에만 토글
    space_down = _input_iface.get_keyboard_value(_keyboard, carb.input.KeyboardInput.SPACE) > 0.5
    if space_down and not _space_was_down:
        _paused = not _paused
        if _paused:
            world.pause()
            print("\n[Sim] 일시정지 (Space: 재개)")
        else:
            world.play()
            print("\n[Sim] 재개")
    _space_was_down = space_down

    if _paused:
        continue

    dt = world.get_physics_dt()

    for agv in agvs.values():
        # 수정 73 — 실물이 붙으면 그때 화면에 등장시킨다.
        # 주행 중에 2번째 AGV가 연결돼도 그 순간 나타난다.
        if TWIN_MODE and agv._twin_seen and not agv._twin_visible:
            agv._twin_visible = True
            set_agv_visible(stage, agv.rid, True)
            print(f"[AGV {agv.rid}] (트윈) ★ 실물 연결 감지 — 화면에 등장 "
                  f"(홈 노드 {agv.current_node})")

        # 트윈: 실물 마커 보고 → 위치 동기화 (명령 실행보다 먼저 — 위치가 최신이어야 함)
        if agv._pending_marker is not None:
            nid = agv._pending_marker
            agv._pending_marker = None
            agv.sync_to_node(nid, stage)

        # 트윈: 실물의 회전/리프트 완료(cmd_ack) → 홀드 해제 + 실측 (수정 60)
        if agv._pending_ack is not None:
            ack_cmd = agv._pending_ack
            agv._pending_ack = None
            agv.on_real_ack(ack_cmd, stage)

        # MQTT 스레드 → main loop: IDLE 상태일 때만 명령 실행
        # (이동/회전 중 명령 수신 시 현재 동작 완료 후 실행)
        if agv._pending_cmd is not None and agv.state == "IDLE":
            cmd      = agv._pending_cmd
            shelf_id = agv._pending_shelf_id
            target   = agv._pending_target_node
            agv._pending_cmd          = None
            agv._pending_shelf_id     = None
            agv._pending_target_node  = None
            agv.execute_cmd(cmd, shelf_id, target)

        agv.update(dt, stage)

    for agv in agvs.values():
        agv.poll_camera()

for bridge in bridges.values():
    bridge.disconnect()
simulation_app.close()
