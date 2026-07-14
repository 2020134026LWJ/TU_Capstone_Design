"""
Step 7 — 발표용 수동 제어 데모 (서버/MQTT 없이, 터미널로 노드 지정)

노드 구조 (6×8=48노드, 위에서부터 0-based 재번호 — 노드=마커 ID=0~47):
  y=4.5: [ 0  1  2  3  4  5  6  7]
  y=3.5: [ 8  9 10 11 12 13 14 15]  ← W2=8  (AGV1 홈)
  y=2.5: [16 17 18 19 20 21 22 23]   (선반: 18 19 21 22)
  y=1.5: [24 25 26 27 28 29 30 31]   (선반: 26 27 29 30)
  y=0.5: [32 33 34 35 36 37 38 39]  ← W1=32 (AGV2 홈)
  y=-0.5:[40 41 42 43 44 45 46 47]
  선반 노드: 18 19 21 22 26 27 29 30

터미널 명령어:
  1 [노드]        AGV1을 해당 노드로 이동  (예: 1 11)
  2 [노드]        AGV2를 해당 노드로 이동  (예: 2 24)
  1 up / 1 down   AGV1 리프트 올리기/내리기
  2 up / 2 down   AGV2 리프트 올리기/내리기
  q               종료

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh \\
        /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_manual.py
"""

from isaacsim import SimulationApp
simulation_app = SimulationApp({"headless": False})

import carb
import carb.input
carb.settings.get_settings().set("/persistent/app/viewport/camLightEnabled", True)
carb.settings.get_settings().set("/rtx/backgroundColorEnabled", True)
carb.settings.get_settings().set("/rtx/backgroundColor", [1.0, 1.0, 1.0, 1.0])

import json
import os
import sys
import queue
import threading
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pxr import UsdGeom, UsdShade, UsdLux, Sdf, Vt, Gf
import omni.usd
import omni.appwindow

from isaacsim.core.api import World
from isaacsim.core.api.objects import VisualCuboid, VisualCylinder, VisualSphere
from isaacsim.core.utils.viewports import set_camera_view

from isaac_hw import IsaacMotors
from camera import IsaacCamera

# ─── CAD 경로 ─────────────────────────────────────────────────────────────────
CAD_PATHS = {"agv": None, "shelf": None, "workstation": None}

# ══════════════════════════════════════════════════════════════════════════════
# ★ 수정 포인트 1 — 이동/리프트 속도
# ══════════════════════════════════════════════════════════════════════════════
# MOVE_SPEED    : AGV 직진 속도 (m/s). 높을수록 빠름 (권장 범위: 0.5 ~ 3.0)
# LIFT_SPEED    : 리프트 상승/하강 속도 (m/s). (권장 범위: 0.1 ~ 0.5)
# HEADING_SPEED : 제자리 회전 각속도 (rad/s). 높을수록 빠르게 회전
# DETECT_INTERVAL: 마커 감지 주기 (프레임 수). 작을수록 더 자주 감지
# ─────────────────────────────────────────────────────────────────────────────
MOVE_SPEED         = 1.5    # ← 이동 속도 조절
POSITION_TOLERANCE = 0.05
LIFT_SPEED         = 0.3    # ← 리프트 속도 조절
WHEEL_RADIUS       = 0.06
WHEEL_BASE         = 0.44
HEADING_SPEED      = 6.0    # ← 회전 속도 조절
TURN_SPEED         = HEADING_SPEED * WHEEL_BASE / 2.0
ANGLE_TOLERANCE    = 0.03
DETECT_INTERVAL    = 5      # ← 마커 감지 주기 조절
CAM_DETECT_RADIUS  = 0.087
ARUCO_DIR = os.path.join(_ROOT, "isaac_simulation", "aruco_markers")


# ═══════════════════════════════════════════════════════════════════════════════
# 맵 데이터 (6×8=48 노드, 0~47 — 선반/작업대/홈은 server/data JSON에서 로드)
# ═══════════════════════════════════════════════════════════════════════════════
#
# 노드 번호 규칙:  node_id = row_idx * 8 + col + 1  (위에서부터 순서대로)
#
#   row_idx | y 좌표 | 노드 범위
#   --------+--------+----------
#       0   |  4.5   |  0~ 7
#       1   |  3.5   |  8~15   ← W2 (col=0 → node 8)
#       2   |  2.5   | 16~23
#       3   |  1.5   | 24~31
#       4   |  0.5   | 32~39   ← W1 (col=0 → node 32)
#       5   | -0.5   | 40~47
#
# 선반: y=2.5(row_idx=2) col=2,3,5,6 → 18,19,21,22
#        y=1.5(row_idx=3) col=2,3,5,6 → 26,27,29,30
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# ★ 수정 포인트 2 — 맵 구성 (선반 위치, 작업대 위치, AGV 시작 위치)
# ══════════════════════════════════════════════════════════════════════════════
# _SHELF_SET : 선반이 놓일 노드 번호 집합. 여기 있는 노드에 선반 오브젝트가 생성됨.
#              선반 위치를 바꾸고 싶으면 노드 번호를 교체.
#              예) {18, 19} → 선반 2개만 배치
#
# _WS_SET    : 작업대(컨베이어+작업자)가 배치될 노드 번호 집합.
#              예) {8, 32} → 현재 W2=8, W1=32
#
# _AGV_HOMES : AGV가 시작할 홈 노드.  {AGV번호: 시작노드}
#              예) {1: 8, 2: 32} → robot_config.json 기준 (AGV1=W2, AGV2=W1)
# ─────────────────────────────────────────────────────────────────────────────
_ROW_Y      = [4.5, 3.5, 2.5, 1.5, 0.5, -0.5]  # row_idx 0~5 (위→아래, 수정 불필요)

# 선반/작업대/홈 노드는 **서버와 같은 JSON**에서 읽는다 (하드코딩 금지 — 두 벌이 되면 어긋난다).
_CFG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "server", "data")
with open(os.path.join(_CFG_DIR, "map.json"), encoding="utf-8") as _f:
    _MAP_CFG = json.load(_f)
with open(os.path.join(_CFG_DIR, "robot_config.json"), encoding="utf-8") as _f:
    _ROBOT_CFG = json.load(_f)

_SHELF_SET  = set(_MAP_CFG["shelf_nodes"])                    # 선반 노드
_WS_SET     = set(_MAP_CFG["workstation_nodes"])              # 작업대 노드
_AGV_HOMES  = {int(rid): info["home_node"]                    # AGV 시작 노드
               for rid, info in _ROBOT_CFG["robots"].items()}


def _make_nodes() -> dict[int, dict]:
    nodes = {}
    for ri in range(6):
        for col in range(8):
            nid = ri * 8 + col          # 노드 = 마커 = 0~47 (0-based)
            x   = col + 0.5
            y   = _ROW_Y[ri]
            if nid in _SHELF_SET:
                ntype = "S"
            elif nid in _WS_SET:
                ntype = "W"
            else:
                ntype = "M"
            nodes[nid] = {"id": nid, "row_idx": ri, "col": col, "x": x, "y": y, "type": ntype}
    return nodes


def _make_adjacency(nodes: dict) -> dict[int, list[int]]:
    adj: dict[int, list[int]] = {}

    def _add(a, b):
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    # 수평 엣지 (같은 row_idx 안에서 col 증가)
    for ri in range(6):
        for col in range(7):
            a = ri * 8 + col
            b = ri * 8 + col + 1
            _add(a, b)

    # 수직 엣지 (row_idx 순서대로 인접, 0↔1↔2↔3↔4↔5)
    VERT_PAIRS = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]
    for ri_lo, ri_hi in VERT_PAIRS:
        for col in range(8):
            a = ri_lo * 8 + col
            b = ri_hi * 8 + col
            _add(a, b)

    return adj


nodes     = _make_nodes()
adjacency = _make_adjacency(nodes)
shelf_node_ids = _SHELF_SET
ws_node_ids    = _WS_SET


def node_xy(nid: int) -> np.ndarray:
    n = nodes[nid]
    return np.array([n["x"], n["y"]], dtype=float)


def _normalize_angle(a: float) -> float:
    while a >  np.pi: a -= 2.0 * np.pi
    while a < -np.pi: a += 2.0 * np.pi
    return a


# ═══════════════════════════════════════════════════════════════════════════════
# BFS 경로 탐색 + 경로→cmd 변환
# ═══════════════════════════════════════════════════════════════════════════════

def bfs_path(start: int, goal: int) -> list[int] | None:
    if start == goal:
        return [start]
    from collections import deque
    q = deque([[start]])
    visited = {start}
    while q:
        path = q.popleft()
        for nxt in adjacency.get(path[-1], []):
            if nxt not in visited:
                new_path = path + [nxt]
                if nxt == goal:
                    return new_path
                visited.add(nxt)
                q.append(new_path)
    return None


def path_to_cmd_seq(path: list[int], start_heading: float) -> list:
    """노드 경로 → [(cmd, wait_type, wait_val)] 시퀀스

    cmd        : "forward" | "turn_left" | "turn_right" | "turn_180"
    wait_type  : "arrived" | "ack"
    wait_val   : node_id   | cmd_str
    """
    seq = []
    heading = start_heading
    for i in range(len(path) - 1):
        curr_xy = node_xy(path[i])
        next_xy = node_xy(path[i + 1])
        direction = float(np.arctan2(next_xy[1] - curr_xy[1],
                                     next_xy[0] - curr_xy[0]))
        diff = _normalize_angle(direction - heading)

        if abs(diff) < 0.15:
            pass  # 직진, 회전 불필요
        elif abs(diff - np.pi / 2) < 0.15:
            seq.append(("turn_left",  "ack", "turn_left"))
        elif abs(diff + np.pi / 2) < 0.15:
            seq.append(("turn_right", "ack", "turn_right"))
        else:
            seq.append(("turn_180",   "ack", "turn_180"))

        seq.append(("forward", "arrived", path[i + 1]))
        heading = direction

    return seq


# ═══════════════════════════════════════════════════════════════════════════════
# IsaacAGV (step6_visual.py 동일)
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
        self.current_node = home_node
        self.motors       = IsaacMotors()

        self.state          = "IDLE"
        self.heading        = 0.0          # East (오른쪽 방향)
        self.heading_target = 0.0

        home = node_xy(home_node)
        self.pos             = home.copy()
        self.target_pos      = home.copy()
        self._moving_to_node = home_node

        self.lift_z         = self.LIFT_PLATE_Z
        self.lift_target_z  = self.LIFT_PLATE_Z
        self.lift_state     = "IDLE"
        self.carrying_shelf = None
        self.wheel_angle    = 0.0
        self._use_cad       = bool(CAD_PATHS.get("agv"))

        self.bridge = None
        self.camera = None
        self._last_marker: int | None = None
        self._pending_cmd: str | None = None
        self._current_turn_cmd: str | None = None

    def set_bridge(self, bridge): self.bridge = bridge
    def set_camera(self, camera): self.camera = camera

    def _on_cmd_from_bridge(self, rid: int, cmd: str):
        self._pending_cmd = cmd

    def poll_camera(self):
        if self.camera is None or self.bridge is None:
            return
        if self.state not in ("MOVING", "IDLE"):
            return
        marker_id, heading_deg = self.camera.detect()
        if marker_id is not None:
            self.bridge.publish_marker(marker_id, heading_deg)

    def execute_cmd(self, cmd: str):
        if cmd == "forward":
            target = self._find_forward_target()
            if target is None:
                print(f"[AGV{self.rid}] forward: 앞에 노드 없음 (heading={self.heading:.2f}rad)")
                return
            self._moving_to_node = target
            self.target_pos = node_xy(target)
            self._last_marker = self.current_node
            if self.camera:
                self.camera.set_last_marker(self.current_node)
            self.state = "MOVING"
            print(f"[AGV{self.rid}] forward → node {target}")

        elif cmd == "turn_left":
            self._current_turn_cmd = cmd
            self.heading_target    = _normalize_angle(self.heading + np.pi / 2)
            self.motors.stop()
            self.state = "TURNING"
            print(f"[AGV{self.rid}] turn_left")

        elif cmd == "turn_right":
            self._current_turn_cmd = cmd
            self.heading_target    = _normalize_angle(self.heading - np.pi / 2)
            self.motors.stop()
            self.state = "TURNING"
            print(f"[AGV{self.rid}] turn_right")

        elif cmd == "turn_180":
            self._current_turn_cmd = cmd
            self.heading_target    = _normalize_angle(self.heading + np.pi)
            self.motors.stop()
            self.state = "TURNING"
            print(f"[AGV{self.rid}] turn_180")

        elif cmd == "lift_up":
            shelf_id = self._find_nearby_shelf()
            self.carrying_shelf = shelf_id
            self.lift_target_z  = self.LIFT_PLATE_UP
            self.lift_state     = "RAISING"
            print(f"[AGV{self.rid}] lift_up (shelf={shelf_id})")

        elif cmd == "lift_down":
            self.lift_target_z = self.LIFT_PLATE_Z
            self.lift_state    = "LOWERING"
            print(f"[AGV{self.rid}] lift_down (shelf={self.carrying_shelf})")

    def _find_forward_target(self) -> int | None:
        dx_h = float(np.cos(self.heading))
        dy_h = float(np.sin(self.heading))
        cx, cy = node_xy(self.current_node)
        best_node, best_score = None, 0.7
        for nid in adjacency.get(self.current_node, []):
            n = nodes[nid]
            dx, dy = n["x"] - cx, n["y"] - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < 0.01:
                continue
            score = (dx * dx_h + dy * dy_h) / dist
            if score > best_score:
                best_score, best_node = score, nid
        return best_node

    def _find_nearby_shelf(self) -> int | None:
        # shelf_origins 에는 lift_down 후 실제 놓인 위치가 갱신됨
        # (원래 노드 좌표가 아니라 현재 실제 위치로 탐색해야 WS에 내려놓은 선반도 찾을 수 있음)
        best_nid, best_dist = None, POSITION_TOLERANCE * 3
        for nid, (ox, oy) in shelf_origins.items():
            dist = float(np.linalg.norm(self.pos - np.array([ox, oy])))
            if dist < best_dist:
                best_dist, best_nid = dist, nid
        return best_nid

    def update(self, dt: float, stage):
        self._update_move(dt, stage)
        self._update_lift(dt, stage)

    def _update_move(self, dt: float, stage):
        if self.state == "TURNING":
            diff = _normalize_angle(self.heading_target - self.heading)
            if abs(diff) < ANGLE_TOLERANCE:
                self.motors.stop()
                self.heading = self.heading_target
                self.state   = "IDLE"
                self._sync_prim(stage)
                turn_cmd = self._current_turn_cmd
                self._current_turn_cmd = None
                if self.bridge and turn_cmd:
                    self.bridge.publish_cmd_ack(turn_cmd)
            else:
                speed = min(1.0, abs(diff) / 0.5) * TURN_SPEED
                if diff > 0:
                    self.motors.set_speeds(-speed,  speed)
                else:
                    self.motors.set_speeds( speed, -speed)
                _, omega = self.motors.get_velocity()
                self.heading = _normalize_angle(self.heading + omega * dt)
                self._sync_prim(stage)

        elif self.state == "MOVING":
            diff = self.target_pos - self.pos
            dist = float(np.linalg.norm(diff))
            if dist < POSITION_TOLERANCE:
                self.motors.stop()
                self.pos          = self.target_pos.copy()
                self.current_node = self._moving_to_node
                self.state        = "IDLE"
                self._sync_prim(stage)
            else:
                angle_to_target = float(np.arctan2(diff[1], diff[0]))
                err = _normalize_angle(angle_to_target - self.heading)
                correction = max(-0.5, min(0.5, err * 2.0))
                self.motors.set_speeds(MOVE_SPEED - correction, MOVE_SPEED + correction)
                v, omega = self.motors.get_velocity()
                self.heading = _normalize_angle(self.heading + omega * dt)
                self.pos += v * dt * np.array([np.cos(self.heading), np.sin(self.heading)])
                self.wheel_angle += abs(v) * dt / WHEEL_RADIUS
                self._sync_prim(stage)

    def _update_lift(self, dt: float, stage):
        if self.lift_state == "IDLE":
            return
        diff = self.lift_target_z - self.lift_z
        step = min(LIFT_SPEED * dt, abs(diff))
        if abs(diff) < 0.005:
            self.lift_z     = self.lift_target_z
            prev_state      = self.lift_state
            self.lift_state = "IDLE"
            self._sync_lift(stage)
            if prev_state == "RAISING":
                print(f"[AGV{self.rid}] lift UP 완료 (shelf={self.carrying_shelf})")
                if self.bridge:
                    self.bridge.publish_cmd_ack("lift_up")
            elif prev_state == "LOWERING":
                shelf_id = self.carrying_shelf
                self.carrying_shelf = None
                self._place_shelf(stage, shelf_id)
                print(f"[AGV{self.rid}] lift DOWN 완료 (shelf={shelf_id})")
                if self.bridge:
                    self.bridge.publish_cmd_ack("lift_down")
        else:
            self.lift_z += step if diff > 0 else -step
            self._sync_lift(stage)

    # ─── USD 동기화 ──────────────────────────────────────────────────────────

    def _sync_prim(self, stage):
        x, y = float(self.pos[0]), float(self.pos[1])
        h = self.heading
        cos_h, sin_h = np.cos(h), np.sin(h)

        if self._use_cad:
            self._set_translate(stage, f"/World/AGV_{self.rid}", x, y, 0.0)
            self._set_orient_z(stage, f"/World/AGV_{self.rid}", h)
            if self.carrying_shelf is not None:
                self._sync_shelf(stage)
            return

        self._set_translate(stage, f"/World/AGV_{self.rid}_body", x, y, self.BODY_Z)
        self._set_orient_z(stage, f"/World/AGV_{self.rid}_body", h)
        self._set_translate(stage, f"/World/AGV_{self.rid}_led",  x, y, 0.155)

        q_wheel = self._wheel_quat(self.wheel_angle, self.heading)
        for i, (wdx, wdy) in enumerate(self.WHEEL_OFFSETS):
            rx = wdx * cos_h - wdy * sin_h
            ry = wdx * sin_h + wdy * cos_h
            self._set_translate(stage, f"/World/AGV_{self.rid}_wheel_{i}",
                                x + rx, y + ry, self.WHEEL_Z)
            self._set_orient_q(stage, f"/World/AGV_{self.rid}_wheel_{i}", q_wheel)

        sc_z, sc_tilt = self._scissor_state(self.lift_z)
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_a",
                            x - 0.06 * sin_h, y + 0.06 * cos_h, sc_z)
        self._set_orient_q(stage, f"/World/AGV_{self.rid}_scissor_a",
                           self._scissor_quat(h, sc_tilt, +1))
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_b",
                            x + 0.06 * sin_h, y - 0.06 * cos_h, sc_z)
        self._set_orient_q(stage, f"/World/AGV_{self.rid}_scissor_b",
                           self._scissor_quat(h, sc_tilt, -1))
        self._set_translate(stage, f"/World/AGV_{self.rid}_lift_plate", x, y, self.lift_z)

        if self.carrying_shelf is not None:
            self._sync_shelf(stage)

    def _sync_shelf(self, stage):
        if self.carrying_shelf is None:
            return
        x, y = float(self.pos[0]), float(self.pos[1])
        dz = max(0.0, self.lift_z - self.LIFT_CONTACT_Z)
        half = self.heading / 2.0
        cw, sw = float(np.cos(half)), float(np.sin(half))
        shelf_root = f"/World/Shelf_{self.carrying_shelf}"
        prim = stage.GetPrimAtPath(shelf_root)
        if not prim.IsValid():
            return
        xform = UsdGeom.Xformable(prim)
        has_t, has_o = False, False
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(x, y, dz)); has_t = True
            elif op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                self._set_quat_op(op, cw, 0.0, 0.0, sw); has_o = True
        if not has_t: xform.AddTranslateOp().Set(Gf.Vec3d(x, y, dz))
        if not has_o:
            xform.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(
                Gf.Quatf(cw, Gf.Vec3f(0.0, 0.0, sw)))

    def _place_shelf(self, stage, shelf_id: int):
        prim = stage.GetPrimAtPath(f"/World/Shelf_{shelf_id}")
        if not prim.IsValid():
            return
        x, y = float(self.pos[0]), float(self.pos[1])
        shelf_origins[shelf_id] = (x, y)
        xform = UsdGeom.Xformable(prim)
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(x, y, 0.0))
            elif op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                self._set_quat_op(op, 1.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _set_quat_op(op, w, x, y, z):
        try:
            op.Set(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
        except Exception:
            op.Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)))

    def _sync_lift(self, stage):
        x, y = float(self.pos[0]), float(self.pos[1])
        h = self.heading
        sin_h, cos_h = np.sin(h), np.cos(h)
        self._set_translate(stage, f"/World/AGV_{self.rid}_lift_plate", x, y, self.lift_z)
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

    @staticmethod
    def _wheel_quat(wheel_angle: float, heading: float) -> Gf.Quatd:
        q_base = Gf.Quatd(0.7071068, Gf.Vec3d(0.7071068, 0.0, 0.0))
        ha = wheel_angle / 2.0
        q_roll = Gf.Quatd(float(np.cos(ha)), Gf.Vec3d(0.0, float(np.sin(ha)), 0.0))
        hh = heading / 2.0
        q_head = Gf.Quatd(float(np.cos(hh)), Gf.Vec3d(0.0, 0.0, float(np.sin(hh))))
        return q_head * q_roll * q_base

    @staticmethod
    def _scissor_state(lift_z: float):
        body_top = 0.14
        lift_bot = lift_z - 0.01
        center_z = (body_top + lift_bot) / 2.0
        arm_half = 0.14
        half_h   = max(0.0, min(arm_half, (lift_bot - body_top) / 2.0))
        tilt     = float(np.arccos(half_h / arm_half))
        return center_z, tilt

    @staticmethod
    def _scissor_quat(heading: float, tilt: float, sign: int) -> Gf.Quatd:
        hh = heading / 2.0
        q_head = Gf.Quatd(float(np.cos(hh)), Gf.Vec3d(0.0, 0.0, float(np.sin(hh))))
        ta = sign * tilt / 2.0
        q_tilt = Gf.Quatd(float(np.cos(ta)), Gf.Vec3d(0.0, float(np.sin(ta)), 0.0))
        return q_head * q_tilt

    @staticmethod
    def _set_translate(stage, path: str, x: float, y: float, z: float):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid(): return
        attr = prim.GetAttribute("xformOp:translate")
        if attr: attr.Set(Gf.Vec3d(x, y, z))

    @staticmethod
    def _set_orient_z(stage, path: str, angle_rad: float):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid(): return
        half = angle_rad / 2.0
        q = Gf.Quatd(float(np.cos(half)), Gf.Vec3d(0.0, 0.0, float(np.sin(half))))
        attr = prim.GetAttribute("xformOp:orient")
        if attr and attr.IsValid(): attr.Set(q)
        else: UsdGeom.Xformable(prim).AddOrientOp().Set(q)

    @staticmethod
    def _set_orient_q(stage, path: str, q: Gf.Quatd):
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid(): return
        attr = prim.GetAttribute("xformOp:orient")
        if attr and attr.IsValid():
            try: attr.Set(q)
            except Exception:
                imag = q.GetImaginary()
                attr.Set(Gf.Quatf(float(q.GetReal()),
                                  Gf.Vec3f(float(imag[0]), float(imag[1]), float(imag[2]))))
        else:
            UsdGeom.Xformable(prim).AddOrientOp().Set(q)


# ═══════════════════════════════════════════════════════════════════════════════
# 색상 팔레트 + 씬 상수
# ═══════════════════════════════════════════════════════════════════════════════

C_FRAME       = np.array([0.20, 0.20, 0.22])
C_SHELF_BOARD = np.array([0.82, 0.74, 0.60])
C_AGV1        = np.array([0.95, 0.32, 0.08])
C_AGV2        = np.array([0.10, 0.38, 0.90])
C_WHEEL       = np.array([0.10, 0.10, 0.10])
C_SCISSOR     = np.array([0.85, 0.68, 0.05])
C_LED         = {1: np.array([1.0, 0.50, 0.0]), 2: np.array([0.0, 0.60, 1.0])}
C_TABLE_LEG   = np.array([0.58, 0.60, 0.63])
C_TRAY        = np.array([0.65, 0.65, 0.70])
C_WORKER_SUIT = np.array([0.98, 0.80, 0.10])
C_WORKER_HEAD = np.array([0.92, 0.72, 0.56])
C_BOX = [
    np.array([0.72, 0.52, 0.04]),
    np.array([0.20, 0.45, 0.72]),
    np.array([0.85, 0.15, 0.10]),
    np.array([0.15, 0.65, 0.15]),
    np.array([0.70, 0.70, 0.70]),
]

WHEEL_QUAT     = np.array([0.7071, 0.7071, 0.0, 0.0])
SCISSOR_QUAT_A = np.array([0.8225, 0.0,  0.5688, 0.0])
SCISSOR_QUAT_B = np.array([0.8225, 0.0, -0.5688, 0.0])

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
# 씬 구성 헬퍼 (step6_visual.py 동일)
# ═══════════════════════════════════════════════════════════════════════════════

def build_shelf(stage, node_id: int, x: float, y: float):
    root = f"/World/Shelf_{node_id}"
    UsdGeom.Xform.Define(stage, root)
    shelf_origins[node_id] = (x, y)

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
        for bname, px, py, sx, sy in [
            ("bf",  0.0,        -LEG_OFFSET, beam_len, beam_thk),
            ("bb",  0.0,         LEG_OFFSET, beam_len, beam_thk),
            ("bl", -LEG_OFFSET,  0.0,        beam_thk, beam_len),
            ("br",  LEG_OFFSET,  0.0,        beam_thk, beam_len),
        ]:
            VisualCuboid(
                prim_path=f"{root}/{bname}_{i}", name=f"shelf_{node_id}_{bname}_{i}",
                position=np.array([px, py, beam_z]),
                scale=np.array([sx, sy, beam_thk]), color=C_FRAME,
            )
    TRAY_W, TRAY_D, TRAY_BH, TRAY_WH, TRAY_WT = 0.58, 0.58, 0.012, 0.065, 0.025
    for lvl, bz in enumerate(BOARD_ZS):
        base_z = bz + BOARD_THK / 2
        VisualCuboid(
            prim_path=f"{root}/tray_bot_{lvl}", name=f"shelf_{node_id}_tray_bot_{lvl}",
            position=np.array([0.0, 0.0, base_z + TRAY_BH / 2]),
            scale=np.array([TRAY_W, TRAY_D, TRAY_BH]), color=C_TRAY,
        )
        inner_d = TRAY_D - TRAY_WT * 2
        for wn, px, py, sx, sy in [
            ("wf", 0.0,                        -(TRAY_D/2-TRAY_WT/2), TRAY_W, TRAY_WT),
            ("wb", 0.0,                          (TRAY_D/2-TRAY_WT/2), TRAY_W, TRAY_WT),
            ("wl", -(TRAY_W/2-TRAY_WT/2), 0.0,  TRAY_WT, inner_d),
            ("wr",  (TRAY_W/2-TRAY_WT/2), 0.0,  TRAY_WT, inner_d),
        ]:
            VisualCuboid(
                prim_path=f"{root}/tray_{wn}_{lvl}", name=f"shelf_{node_id}_tray_{wn}_{lvl}",
                position=np.array([px, py, base_z + TRAY_BH + TRAY_WH / 2]),
                scale=np.array([sx, sy, TRAY_WH]), color=C_TRAY,
            )
        rng = np.random.RandomState(node_id * 31 + lvl * 7)
        n_items = int(rng.randint(4, 7))
        inner_r = TRAY_W / 2 - TRAY_WT - 0.07
        for ii in range(n_items):
            iw = float(rng.uniform(0.07, 0.13))
            id_ = float(rng.uniform(0.06, 0.12))
            ih = float(rng.uniform(0.07, 0.16))
            ix = float(rng.uniform(-inner_r, inner_r))
            iy = float(rng.uniform(-inner_r, inner_r))
            color = C_BOX[int(rng.randint(0, len(C_BOX)))]
            VisualCuboid(
                prim_path=f"{root}/item_{lvl}_{ii}", name=f"shelf_{node_id}_item_{lvl}_{ii}",
                position=np.array([ix, iy, base_z + TRAY_BH + ih / 2]),
                scale=np.array([iw, id_, ih]), color=color,
            )


def build_workstation(stage, node_id: int, x: float, y: float):
    root = f"/World/WS_{node_id}"
    UsdGeom.Xform.Define(stage, root)
    CONV_H, CONV_LX, CONV_W = 0.80, 1.30, 0.42
    CONV_CX = x - 1.10
    CONV_CY = y
    for si, sy_off in enumerate([-1, 1]):
        VisualCuboid(
            prim_path=f"{root}/side_{si}", name=f"ws_{node_id}_side_{si}",
            position=np.array([CONV_CX, CONV_CY + sy_off * CONV_W / 2, CONV_H]),
            scale=np.array([CONV_LX, 0.04, 0.07]), color=C_TABLE_LEG,
        )
    for li, (ldx, ldy) in enumerate([(-1,-1),(1,-1),(-1,1),(1,1)]):
        VisualCylinder(
            prim_path=f"{root}/leg_{li}", name=f"ws_{node_id}_leg_{li}",
            position=np.array([CONV_CX + ldx*(CONV_LX/2-0.06),
                               CONV_CY + ldy*(CONV_W/2-0.04), (CONV_H-0.03)/2]),
            radius=0.025, height=CONV_H-0.03, color=C_TABLE_LEG,
        )
    ROLLER_Q = np.array([0.7071, 0.7071, 0.0, 0.0])
    for ri in range(8):
        rx = CONV_CX - CONV_LX/2 + 0.08 + ri*(CONV_LX-0.16)/7
        VisualCylinder(
            prim_path=f"{root}/roller_{ri}", name=f"ws_{node_id}_roller_{ri}",
            position=np.array([rx, CONV_CY, CONV_H+0.022]),
            orientation=ROLLER_Q, radius=0.022, height=CONV_W-0.06,
            color=np.array([0.75, 0.75, 0.78]),
        )
    bin_x = CONV_CX - CONV_LX/2 - 0.28
    VisualCuboid(
        prim_path=f"{root}/out_bin", name=f"ws_{node_id}_out_bin",
        position=np.array([bin_x, CONV_CY, 0.25]),
        scale=np.array([0.44, 0.58, 0.46]), color=np.array([0.30, 0.30, 0.33]),
    )
    VisualCuboid(
        prim_path=f"{root}/out_items", name=f"ws_{node_id}_out_items",
        position=np.array([bin_x, CONV_CY, 0.50]),
        scale=np.array([0.38, 0.50, 0.06]), color=np.array([0.85, 0.75, 0.20]),
    )
    VisualCylinder(
        prim_path=f"{root}/status_led", name=f"ws_{node_id}_led",
        position=np.array([CONV_CX+CONV_LX/2-0.1, CONV_CY+CONV_W/2, CONV_H+0.10]),
        radius=0.022, height=0.05, color=np.array([0.1, 0.9, 0.1]),
    )


def build_worker(stage, ws_node_id: int, x: float, y: float):
    root = f"/World/Worker_{ws_node_id}"
    UsdGeom.Xform.Define(stage, root)
    wx, wy = x - 1.10, y + 0.50
    VisualCylinder(
        prim_path=f"{root}/legs", name=f"worker_{ws_node_id}_legs",
        position=np.array([wx, wy, 0.30]), radius=0.10, height=0.55,
        color=np.array([0.20, 0.25, 0.55]),
    )
    VisualCylinder(
        prim_path=f"{root}/torso", name=f"worker_{ws_node_id}_torso",
        position=np.array([wx, wy, 0.72]), radius=0.12, height=0.44, color=C_WORKER_SUIT,
    )
    VisualSphere(
        prim_path=f"{root}/head", name=f"worker_{ws_node_id}_head",
        position=np.array([wx, wy, 1.07]), radius=0.12, color=C_WORKER_HEAD,
    )
    VisualCylinder(
        prim_path=f"{root}/helmet", name=f"worker_{ws_node_id}_helmet",
        position=np.array([wx, wy, 1.17]), radius=0.15, height=0.07,
        color=np.array([1.0, 0.85, 0.0]),
    )


def build_agv(stage, rid: int, x: float, y: float):
    c_agv = {1: C_AGV1, 2: C_AGV2}.get(rid, C_AGV1)
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_body", name=f"agv_{rid}_body",
        position=np.array([x, y, IsaacAGV.BODY_Z]),
        scale=np.array([0.38, 0.38, 0.08]), color=c_agv,
    )
    for i, (dx, dy) in enumerate(IsaacAGV.WHEEL_OFFSETS):
        VisualCylinder(
            prim_path=f"/World/AGV_{rid}_wheel_{i}", name=f"agv_{rid}_wheel_{i}",
            position=np.array([x+dx, y+dy, IsaacAGV.WHEEL_Z]),
            orientation=WHEEL_QUAT, radius=0.06, height=0.05, color=C_WHEEL,
        )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_a", name=f"agv_{rid}_scissor_a",
        position=np.array([x, y+0.06, IsaacAGV.SCISSOR_Z]),
        orientation=SCISSOR_QUAT_A, scale=np.array([0.025, 0.04, 0.28]), color=C_SCISSOR,
    )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_b", name=f"agv_{rid}_scissor_b",
        position=np.array([x, y-0.06, IsaacAGV.SCISSOR_Z]),
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


def make_aruco_material(stage, node_id: int) -> UsdShade.Material:
    mat_path = f"/World/ArUcoMats/mat_{node_id}"
    material = UsdShade.Material.Define(stage, mat_path)
    shader   = UsdShade.Shader.Define(stage, f"{mat_path}/shader")
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
        Gf.Vec3f(x-h, y-h, MARKER_Z), Gf.Vec3f(x+h, y-h, MARKER_Z),
        Gf.Vec3f(x+h, y+h, MARKER_Z), Gf.Vec3f(x-h, y+h, MARKER_Z),
    ]))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([4]))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray([0, 1, 2, 3]))
    mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * 4))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    st_pv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying)
    st_pv.Set(Vt.Vec2fArray([
        Gf.Vec2f(0,0), Gf.Vec2f(1,0), Gf.Vec2f(1,1), Gf.Vec2f(0,1),
    ]))
    UsdShade.MaterialBindingAPI(mesh.GetPrim()).Bind(
        make_aruco_material(stage, node_id))


def _heading_after_path(path: list, start_heading: float) -> float:
    """경로를 따라 이동했을 때 최종 heading을 계산 (cmd 시퀀스 예측용)"""
    heading = start_heading
    for i in range(len(path) - 1):
        curr_xy = node_xy(path[i])
        next_xy = node_xy(path[i + 1])
        heading = float(np.arctan2(next_xy[1] - curr_xy[1], next_xy[0] - curr_xy[0]))
    return heading


# ═══════════════════════════════════════════════════════════════════════════════
# DemoLocalBridge — MQTT 없이 직접 콜백
# ═══════════════════════════════════════════════════════════════════════════════

class DemoLocalBridge:
    def __init__(self, rid, on_marker_fn, on_cmd_ack_fn):
        self.rid         = rid
        self._on_marker  = on_marker_fn
        self._on_ack     = on_cmd_ack_fn

    def publish_marker(self, marker_id: int, heading_deg: float):
        self._on_marker(self.rid, marker_id)

    def publish_cmd_ack(self, cmd: str):
        self._on_ack(self.rid, cmd)

    def connect(self):    pass
    def disconnect(self): pass


# ═══════════════════════════════════════════════════════════════════════════════
# ManualController — 터미널 명령으로 AGV 이동 제어
# ═══════════════════════════════════════════════════════════════════════════════

class ManualController:
    """BFS 경로 계획 + cmd 시퀀스 실행 (마커/ack/타이머 이벤트 기반)"""

    # ★ WS 대기 시간 (초) — 선반을 작업대에 내려놓고 기다리는 시간
    WAIT_AT_WS = 1.0   # ← 여기서 대기 시간 변경 (단위: 초)

    def __init__(self, agvs: dict):
        self.agvs      = agvs
        self._sim_time = 0.0
        # per-AGV 상태
        self._states = {
            rid: {"seq": [], "idx": 0, "busy": False, "target": None, "wait_end": 0.0}
            for rid in agvs
        }

    # ─────────────────────────────────────────────────────────────────────────
    # 공개 메서드
    # ─────────────────────────────────────────────────────────────────────────

    def pick_and_return(self, rid: int, shelf_node: int):
        """선반 피킹 전체 시퀀스:
          1) 현재위치 → shelf_node  이동
          2) lift_up  (선반 픽업)
          3) shelf_node → WS(홈)   이동
          4) lift_down (선반 내려놓기)
          5) WAIT_AT_WS 초 대기
          6) lift_up  (다시 픽업)
          7) WS → shelf_node       이동 (반납)
          8) lift_down (선반 반납 완료)
          9) shelf_node → WS(홈)   복귀
        """
        agv = self.agvs.get(rid)
        if agv is None:
            print(f"[Ctrl] AGV{rid} 없음"); return

        state = self._states[rid]
        if state["busy"]:
            print(f"[Ctrl] AGV{rid} 이동 중 (목표: node {state['target']}). 완료 후 재시도."); return

        if shelf_node not in nodes:
            print(f"[Ctrl] node {shelf_node} 없음 (유효 범위: 0~47)"); return

        home_node = _AGV_HOMES[rid]
        seq = []
        h = agv.heading   # 각 구간 종료 후 예상 heading 추적

        # Phase 1: 현재 → shelf_node
        p1 = bfs_path(agv.current_node, shelf_node)
        if p1 is None:
            print(f"[Ctrl] AGV{rid}: node {agv.current_node}→{shelf_node} 경로 없음!"); return
        seq += path_to_cmd_seq(p1, h)
        h = _heading_after_path(p1, h)

        # Phase 2: lift_up
        seq.append(("lift_up", "ack", "lift_up"))

        # Phase 3: shelf_node → home(WS)
        p2 = bfs_path(shelf_node, home_node)
        if p2 is None:
            print(f"[Ctrl] AGV{rid}: node {shelf_node}→{home_node} 경로 없음!"); return
        seq += path_to_cmd_seq(p2, h)
        h = _heading_after_path(p2, h)

        # Phase 4: lift_down (WS에 내려놓기)
        seq.append(("lift_down", "ack", "lift_down"))

        # Phase 5: 대기
        seq.append((None, "timer", self.WAIT_AT_WS))

        # Phase 6: lift_up (다시 픽업)
        seq.append(("lift_up", "ack", "lift_up"))

        # Phase 7: home(WS) → shelf_node (반납 이동)
        p3 = bfs_path(home_node, shelf_node)
        if p3 is None:
            print(f"[Ctrl] AGV{rid}: node {home_node}→{shelf_node} 경로 없음!"); return
        seq += path_to_cmd_seq(p3, h)
        h = _heading_after_path(p3, h)

        # Phase 8: lift_down (선반 반납 완료)
        seq.append(("lift_down", "ack", "lift_down"))

        # Phase 9: shelf_node → home 복귀
        p4 = bfs_path(shelf_node, home_node)
        seq += path_to_cmd_seq(p4, h)

        state["seq"]      = seq
        state["idx"]      = 0
        state["busy"]     = True
        state["target"]   = shelf_node
        state["wait_end"] = 0.0
        print(f"[Ctrl] AGV{rid} 선반 피킹 시퀀스 시작: node {shelf_node} → W{rid} → node {shelf_node} → 홈")
        self._execute_next(rid)

    def move_to(self, rid: int, target: int):
        """AGV를 target 노드로 단순 이동 (선반 조작 없음)"""
        agv = self.agvs.get(rid)
        if agv is None:
            print(f"[Ctrl] AGV{rid} 없음"); return

        state = self._states[rid]
        if state["busy"]:
            print(f"[Ctrl] AGV{rid} 이동 중 (목표: node {state['target']}). 완료 후 재시도."); return

        if target not in nodes:
            print(f"[Ctrl] node {target} 없음 (유효 범위: 0~47)"); return

        start = agv.current_node
        if start == target:
            print(f"[Ctrl] AGV{rid} 이미 node {target}에 있음"); return

        path = bfs_path(start, target)
        if path is None:
            print(f"[Ctrl] AGV{rid}: node {start}→{target} 경로 없음!"); return

        seq = path_to_cmd_seq(path, agv.heading)
        state["seq"]    = seq
        state["idx"]    = 0
        state["busy"]   = True
        state["target"] = target
        print(f"[Ctrl] AGV{rid} → node {target}  경로: {path}")
        self._execute_next(rid)

    def lift(self, rid: int, direction: str):
        """리프트 수동 명령 (up/down)"""
        agv = self.agvs.get(rid)
        if agv is None:
            print(f"[Ctrl] AGV{rid} 없음"); return
        cmd = "lift_up" if direction == "up" else "lift_down"
        agv._pending_cmd = cmd
        print(f"[Ctrl] AGV{rid} {cmd}")

    def update(self, dt: float):
        """매 프레임 호출 — timer 대기 완료 체크"""
        self._sim_time += dt
        for rid, state in self._states.items():
            if not state["busy"] or state["idx"] >= len(state["seq"]):
                continue
            _, wait_type, _ = state["seq"][state["idx"]]
            if wait_type == "timer" and self._sim_time >= state["wait_end"]:
                state["idx"] += 1
                if state["idx"] >= len(state["seq"]):
                    state["busy"] = False
                    print(f"\n  ✓ AGV{rid} 시퀀스 완료!\n>>> ", end="", flush=True)
                    return
                self._execute_next(rid)

    # ─────────────────────────────────────────────────────────────────────────
    # 이벤트 핸들러 (bridge 콜백)
    # ─────────────────────────────────────────────────────────────────────────

    def on_marker(self, rid: int, node_id: int):
        state = self._states[rid]
        if not state["busy"] or state["idx"] >= len(state["seq"]):
            return
        cmd, wait_type, wait_val = state["seq"][state["idx"]]
        if wait_type == "arrived" and wait_val == node_id:
            state["idx"] += 1
            if state["idx"] >= len(state["seq"]):
                state["busy"] = False
                print(f"\n  ✓ AGV{rid}  node {node_id} 도착!\n>>> ", end="", flush=True)
                return
            self._execute_next(rid)

    def on_cmd_ack(self, rid: int, cmd: str):
        state = self._states[rid]
        if not state["busy"] or state["idx"] >= len(state["seq"]):
            return
        _, wait_type, wait_val = state["seq"][state["idx"]]
        if wait_type == "ack" and wait_val == cmd:
            state["idx"] += 1
            if state["idx"] >= len(state["seq"]):
                state["busy"] = False
                print(f"\n  ✓ AGV{rid} 시퀀스 완료!\n>>> ", end="", flush=True)
                return
            self._execute_next(rid)

    # ─────────────────────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_next(self, rid: int):
        state = self._states[rid]
        if state["idx"] >= len(state["seq"]):
            return
        cmd, wait_type, wait_val = state["seq"][state["idx"]]
        if wait_type == "timer":
            # 타이머 시작: 종료 시각 기록 후 대기 (cmd 발행 없음)
            state["wait_end"] = self._sim_time + float(wait_val)
            print(f"[Ctrl] AGV{rid} 대기 {wait_val}초...")
            return
        if cmd is not None:
            self.agvs[rid]._pending_cmd = cmd


# ═══════════════════════════════════════════════════════════════════════════════
# 터미널 입력 스레드
# ═══════════════════════════════════════════════════════════════════════════════

_cmd_queue: queue.Queue = queue.Queue()
_quit_flag = threading.Event()


def _print_help():
    print("""
  ┌──────────────────────────────────────────────┐
  │      AGV 수동 제어 — 터미널 명령어             │
  ├──────────────────────────────────────────────┤
  │  1 [선반노드]   AGV1 자동 피킹 (예: 1 27)      │
  │  2 [선반노드]   AGV2 자동 피킹 (예: 2 19)      │
  │  1 [일반노드]   AGV1 단순 이동 (예: 1 5)       │
  │  1 up           AGV1 리프트 올리기             │
  │  1 down         AGV1 리프트 내리기             │
  │  2 up / 2 down  AGV2 리프트                   │
  │  map            노드 지도 출력                 │
  │  status         AGV 현재 상태 출력             │
  │  q              종료                          │
  ├──────────────────────────────────────────────┤
  │  노드 지도 (위에서부터 순서대로):               │
  │   y=4.5:  0  1  2  3  4  5  6  7             │
  │   y=3.5:  8  9 10 11 12 13 14 15  ← W2=8     │
  │   y=2.5: 16 17[18 19]20[21 22]23             │
  │   y=1.5: 24 25[26 27]28[29 30]31             │
  │   y=0.5: 32 33 34 35 36 37 38 39  ← W1=32    │
  │   y=-0.5:40 41 42 43 44 45 46 47             │
  │   [ ] = 선반 노드: 18 19 21 22 26 27 29 30   │
  └──────────────────────────────────────────────┘""")


# ══════════════════════════════════════════════════════════════════════════════
# ★ 수정 포인트 3 — 터미널 명령어 형식
# ══════════════════════════════════════════════════════════════════════════════
# 현재 지원 명령:
#   "1 27"      → AGV1을 node 27로 이동
#   "2 9"       → AGV2를 node 9(W2)로 이동
#   "1 up"      → AGV1 리프트 올리기
#   "1 down"    → AGV1 리프트 내리기
#   "status"    → AGV 현재 노드/상태 출력
#   "map"       → 노드 지도 출력
#   "q"         → 종료
#
# 명령어를 추가하고 싶으면:
#   1) 아래 elif 블록에 원하는 명령 파싱 추가
#   2) _cmd_queue.put(("명령종류", ...)) 로 메인루프에 전달
#   3) 메인루프 "터미널 명령 처리" 블록에서 해당 종류 처리 추가
# ─────────────────────────────────────────────────────────────────────────────
def _terminal_input_thread(agvs_ref):
    """별도 스레드에서 stdin 읽기"""
    _print_help()
    print("\n>>> ", end="", flush=True)
    while not _quit_flag.is_set():
        try:
            line = input()
        except EOFError:
            break
        line = line.strip().lower()
        if not line:
            print(">>> ", end="", flush=True)
            continue

        if line in ("q", "quit", "exit"):
            _cmd_queue.put(("quit",))
            break
        elif line == "map":
            _print_help()
        elif line == "status":
            _cmd_queue.put(("status",))
        else:
            parts = line.split()
            if len(parts) == 2:
                try:
                    rid = int(parts[0])          # 첫 번째 숫자 = AGV 번호 (1 또는 2)
                    if parts[1] in ("up", "down"):
                        _cmd_queue.put(("lift", rid, parts[1]))
                    else:
                        target = int(parts[1])   # 두 번째 숫자 = 목표 노드 (0~47)
                        if target in _SHELF_SET:
                            _cmd_queue.put(("pick", rid, target))  # 선반 노드 → 자동 피킹 시퀀스
                        else:
                            _cmd_queue.put(("move", rid, target))
                except ValueError:
                    print(f"  잘못된 명령: '{line}'  (help: map)")
            else:
                print(f"  잘못된 명령: '{line}'  (help: map)")

        print(">>> ", end="", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# 씬 구성
# ═══════════════════════════════════════════════════════════════════════════════

world = World(stage_units_in_meters=1.0)
stage = omni.usd.get_context().get_stage()

_dome = UsdLux.DomeLight.Define(stage, "/World/DomeLight")
_dome.CreateIntensityAttr(3000.0)
_dome.CreateColorAttr(Gf.Vec3f(1.0, 1.0, 1.0))
_dome.CreateTextureFileAttr("")

# 바닥: x=[-1.5, 8.5], y=[-1.5, 5.5] 포함 (작업대 컨베이어 x≈-0.6까지)
VisualCuboid(
    prim_path="/World/Floor", name="floor",
    position=np.array([3.5, 2.0, -0.01]),
    scale=np.array([11.0, 8.0, 0.02]),
    color=np.array([0.25, 0.25, 0.25]),
)

# 선반 배치
for nid in shelf_node_ids:
    n = nodes[nid]
    build_shelf(stage, nid, n["x"], n["y"])

# 작업대 + 작업자 배치 (nodes 1, 25)
for nid in ws_node_ids:
    n = nodes[nid]
    build_workstation(stage, nid, n["x"], n["y"])
    build_worker(stage, nid, n["x"], n["y"])

# AGV 배치
agvs: dict[int, IsaacAGV] = {}
for rid, home_node in sorted(_AGV_HOMES.items()):
    n = nodes[home_node]
    build_agv(stage, rid, n["x"], n["y"])
    agvs[rid] = IsaacAGV(rid, home_node)
    print(f"[AGV{rid}] 홈: node {home_node}  ({n['x']}, {n['y']})")

world.reset()

# 선반 루트 위치 (world.reset() 이후 설정)
for nid in shelf_node_ids:
    n = nodes[nid]
    shelf_root_prim = stage.GetPrimAtPath(f"/World/Shelf_{nid}")
    if shelf_root_prim.IsValid():
        UsdGeom.Xformable(shelf_root_prim).AddTranslateOp().Set(
            Gf.Vec3d(n["x"], n["y"], 0.0))

for agv in agvs.values():
    agv._sync_prim(stage)
print("  씬 배치 완료")

# ArUco 마커 (48개 전체)
for nid, n in nodes.items():
    create_aruco_marker(stage, nid, n["x"], n["y"])
print(f"  ArUco 마커 {len(nodes)}개 배치 완료")

# ─── ManualController + DemoLocalBridge + IsaacCamera 초기화 ─────────────────
controller = ManualController(agvs)
all_node_ids = set(nodes.keys())

for agv in agvs.values():
    bridge = DemoLocalBridge(
        rid=agv.rid,
        on_marker_fn=controller.on_marker,
        on_cmd_ack_fn=controller.on_cmd_ack,
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

set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

print()
print("=" * 60)
print("  Step 7 — 수동 제어 데모 (서버/MQTT 없음)")
print("=" * 60)
print(f"  AGV1 홈: node 33  (0.5, 0.5) — W1")
print(f"  AGV2 홈: node  9  (0.5, 3.5) — W2")
print(f"  선반: 19 20 22 23 27 28 30 31")
print("-" * 60)
print("  터미널에 명령 입력 (map: 노드 지도,  q: 종료)")
print("  예) 1 27   → AGV1을 선반 27로 이동")
print("      1 up   → AGV1 리프트 올리기")
print("      1 33   → AGV1을 W1(node 33)으로 이동")
print("=" * 60)
print()

for _ in range(10):
    simulation_app.update()

# ─── 키보드 일시정지 ──────────────────────────────────────────────────────────
_paused = False
_space_was_down = False
_input_iface = carb.input.acquire_input_interface()
_keyboard = omni.appwindow.get_default_app_window().get_keyboard()

# ─── 터미널 입력 스레드 시작 ─────────────────────────────────────────────────
_input_thread = threading.Thread(
    target=_terminal_input_thread,
    args=(agvs,),
    daemon=True,
)
_input_thread.start()

# ─── 메인 루프 ────────────────────────────────────────────────────────────────
_running = True
while simulation_app.is_running() and _running:
    world.step(render=True)

    # Space: 일시정지/재개
    space_down = _input_iface.get_keyboard_value(
        _keyboard, carb.input.KeyboardInput.SPACE) > 0.5
    if space_down and not _space_was_down:
        _paused = not _paused
        if _paused:
            world.pause()
            print("\n[Sim] 일시정지 (Space: 재개)\n>>> ", end="", flush=True)
        else:
            world.play()
            print("\n[Sim] 재개\n>>> ", end="", flush=True)
    _space_was_down = space_down

    if _paused:
        continue

    # 터미널 명령 처리 (메인 루프에서 안전하게 실행)
    while not _cmd_queue.empty():
        item = _cmd_queue.get_nowait()
        if item[0] == "quit":
            _running = False
            break
        elif item[0] == "pick":
            _, rid, target = item
            controller.pick_and_return(rid, target)
        elif item[0] == "move":
            _, rid, target = item
            controller.move_to(rid, target)
        elif item[0] == "lift":
            _, rid, direction = item
            controller.lift(rid, direction)
        elif item[0] == "status":
            for rid, agv in agvs.items():
                s = controller._states[rid]
                carrying = f"선반{agv.carrying_shelf}" if agv.carrying_shelf else "없음"
                busy_str = f"→node {s['target']}" if s["busy"] else "대기중"
                print(f"  AGV{rid}: node {agv.current_node}  {busy_str}  "
                      f"carrying={carrying}  heading={np.degrees(agv.heading):.0f}°")

    dt = world.get_physics_dt()
    controller.update(dt)

    for agv in agvs.values():
        # IDLE + 리프트 IDLE 상태에서만 다음 명령 소비
        if (agv._pending_cmd is not None
                and agv.state == "IDLE"
                and agv.lift_state == "IDLE"):
            cmd = agv._pending_cmd
            agv._pending_cmd = None
            agv.execute_cmd(cmd)
        agv.update(dt, stage)

    for agv in agvs.values():
        agv.poll_camera()

_quit_flag.set()
simulation_app.close()
