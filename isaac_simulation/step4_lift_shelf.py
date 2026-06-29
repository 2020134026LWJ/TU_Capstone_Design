"""
Step 4 — 리프트 애니메이션 + 선반 이동

구현 내용:
  1. Step 3 씬 + AGV 그대로 유지
  2. /agv/shelf_cmd 수신
     - pickup : 시저리프트 상판 올리기 애니메이션 → 선반 AGV에 부착 → shelf_ack 발행
     - putdown: 시저리프트 상판 내리기 애니메이션 → 선반 원위치 해제 → shelf_ack 발행
  3. AGV 이동 시 부착된 선반도 함께 이동

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh step4_lift_shelf.py
"""

from isaacsim import SimulationApp
simulation_app = SimulationApp({"headless": False})

import carb
carb.settings.get_settings().set("/persistent/app/viewport/camLightEnabled", True)

import json
import os
import time
import numpy as np

from pxr import UsdGeom, UsdShade, Sdf, Vt, Gf
import omni.usd

from isaacsim.core.api import World
from isaacsim.core.api.objects import VisualCuboid, VisualCylinder
from isaacsim.core.utils.viewports import set_camera_view

import paho.mqtt.client as mqtt_lib

# ─── MQTT 설정 ────────────────────────────────────────────────────────────────
MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_PLAN      = "/agv/plan"
TOPIC_ARRIVED   = "/agv/arrived"
TOPIC_CONTROL   = "/agv/control"
TOPIC_MARKER    = "/agv/marker"
TOPIC_SHELF_CMD = "/agv/shelf_cmd"
TOPIC_SHELF_ACK = "/agv/shelf_ack"

# ─── 이동/리프트 파라미터 ─────────────────────────────────────────────────────
MOVE_SPEED         = 1.5    # m/s
POSITION_TOLERANCE = 0.05   # m
LIFT_SPEED         = 0.3    # m/s (리프트 올리기/내리기 속도)

# ─── 설정 파일 경로 ───────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.join(_HERE, "..")
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
staging_nodes  = {int(ws["staging_node"]) for ws in shelf_cfg["workstations"].values()}
trigger_nodes  = {int(ws["trigger_node"]) for ws in shelf_cfg["workstations"].values()}
robot_homes    = {int(rid): info["home_node"]
                  for rid, info in robot_cfg["robots"].items()}


def node_xy(node_id: int) -> np.ndarray:
    n = nodes[node_id]
    return np.array([n["x"], n["y"]], dtype=float)


# ═══════════════════════════════════════════════════════════════════════════════
# IsaacAGV — 이동 + 리프트 상태머신
# ═══════════════════════════════════════════════════════════════════════════════

class IsaacAGV:
    BODY_Z          = 0.10
    WHEEL_Z         = 0.06
    SCISSOR_Z       = 0.195
    LIFT_PLATE_Z    = 0.25    # 리프트 내린 상태 (기본)
    LIFT_PLATE_UP   = 0.42    # 리프트 올린 상태
    LIFT_CONTACT_Z  = 0.3775  # 상판 top이 선반 하판 bottom에 닿는 lift_z
                               # (선반 1층 board center=0.40, 두께=0.025 → bottom=0.3875
                               #  상판 half-height=0.01 → contact = 0.3875 - 0.01 = 0.3775)
    WHEEL_OFFSETS   = [(0.0, +0.22), (0.0, -0.22)]

    def __init__(self, rid: int, home_node: int):
        self.rid          = rid
        self.current_node = home_node

        # 이동 상태
        self.state      = "IDLE"
        self.path_queue = []
        self.goal_node  = None
        self.heading    = 0.0   # 현재 진행 방향 (라디안, Z축 회전)

        home = node_xy(home_node)
        self.pos             = home.copy()
        self.target_pos      = home.copy()
        self._moving_to_node = home_node

        # 리프트 상태
        self.lift_z          = self.LIFT_PLATE_Z   # 현재 상판 높이
        self.lift_target_z   = self.LIFT_PLATE_Z   # 목표 상판 높이
        self.lift_state      = "IDLE"               # IDLE / RAISING / LOWERING
        self.carrying_shelf  = None                 # 들고 있는 선반 노드 ID

        # 콜백
        self.on_arrived      = None
        self.on_intermediate = None
        self.on_lift_done    = None   # fn(rid, shelf_id, command)

    # ─── 이동 ────────────────────────────────────────────────────────────────

    def set_plan(self, path_queue: list, goal: int):
        self.path_queue = list(path_queue)
        self.goal_node  = goal
        self._move_to_next()

    def _move_to_next(self):
        if not self.path_queue:
            return
        next_node            = self.path_queue.pop(0)
        self._moving_to_node = next_node
        self.target_pos      = node_xy(next_node)
        self.state           = "MOVING"

    def resume(self):
        if self.state == "NODE_WAIT" and self.path_queue:
            self._move_to_next()

    # ─── 리프트 명령 ─────────────────────────────────────────────────────────

    def cmd_pickup(self, shelf_id: int):
        """선반 집기: 리프트 올리기 시작"""
        self.carrying_shelf = shelf_id
        self.lift_target_z  = self.LIFT_PLATE_UP
        self.lift_state     = "RAISING"
        print(f"[AGV {self.rid}] Lift RAISING → shelf {shelf_id}")

    def cmd_putdown(self, shelf_id: int):
        """선반 내려놓기: 리프트 내리기 시작"""
        self.lift_target_z = self.LIFT_PLATE_Z
        self.lift_state    = "LOWERING"
        print(f"[AGV {self.rid}] Lift LOWERING → shelf {shelf_id}")

    # ─── 업데이트 ────────────────────────────────────────────────────────────

    def update(self, dt: float, stage):
        self._update_move(dt, stage)
        self._update_lift(dt, stage)

    def _update_move(self, dt: float, stage):
        if self.state != "MOVING":
            return

        diff = self.target_pos - self.pos
        dist = float(np.linalg.norm(diff))

        if dist < POSITION_TOLERANCE:
            self.pos          = self.target_pos.copy()
            self.current_node = self._moving_to_node
            self._sync_prim(stage)
            print(f"[AGV {self.rid}] Reached node {self.current_node}")

            if self.path_queue:
                self.state = "NODE_WAIT"
                if self.on_intermediate:
                    self.on_intermediate(self.rid, self.current_node)
            else:
                self.state     = "IDLE"
                self.goal_node = None
                if self.on_arrived:
                    self.on_arrived(self.rid, self.current_node)
        else:
            self.heading = float(np.arctan2(diff[1], diff[0]))
            step = min(MOVE_SPEED * dt, dist)
            self.pos += (diff / dist) * step
            self._sync_prim(stage)

    def _update_lift(self, dt: float, stage):
        if self.lift_state == "IDLE":
            return

        diff = self.lift_target_z - self.lift_z
        step = min(LIFT_SPEED * dt, abs(diff))

        if abs(diff) < 0.005:
            # 리프트 완료
            self.lift_z     = self.lift_target_z
            prev_state      = self.lift_state
            self.lift_state = "IDLE"
            self._sync_lift(stage)

            if prev_state == "RAISING":
                print(f"[AGV {self.rid}] Lift UP done (shelf {self.carrying_shelf})")
                if self.on_lift_done:
                    self.on_lift_done(self.rid, self.carrying_shelf, "pickup")
            elif prev_state == "LOWERING":
                shelf_id = self.carrying_shelf
                self.carrying_shelf = None
                print(f"[AGV {self.rid}] Lift DOWN done (shelf {shelf_id})")
                if self.on_lift_done:
                    self.on_lift_done(self.rid, shelf_id, "putdown")
        else:
            self.lift_z += step if diff > 0 else -step
            self._sync_lift(stage)

    # ─── USD 동기화 ──────────────────────────────────────────────────────────

    def _sync_prim(self, stage):
        """바디 + 바퀴 + 시저리프트 위치/방향 동기화"""
        x, y = float(self.pos[0]), float(self.pos[1])
        h = self.heading
        cos_h, sin_h = np.cos(h), np.sin(h)

        # 바디 (위치 + Z축 회전)
        self._set_translate(stage, f"/World/AGV_{self.rid}_body", x, y, self.BODY_Z)
        self._set_orient_z(stage, f"/World/AGV_{self.rid}_body", h)

        # 바퀴 (heading 방향으로 offset 회전)
        for i, (wdx, wdy) in enumerate(self.WHEEL_OFFSETS):
            rx = wdx * cos_h - wdy * sin_h
            ry = wdx * sin_h + wdy * cos_h
            self._set_translate(stage, f"/World/AGV_{self.rid}_wheel_{i}",
                                x + rx, y + ry, self.WHEEL_Z)

        # 시저리프트 막대 (offset ±0.06 을 heading으로 회전)
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_a",
                            x - 0.06 * sin_h, y + 0.06 * cos_h, self.SCISSOR_Z)
        self._set_translate(stage, f"/World/AGV_{self.rid}_scissor_b",
                            x + 0.06 * sin_h, y - 0.06 * cos_h, self.SCISSOR_Z)

        # 상판
        self._set_translate(stage, f"/World/AGV_{self.rid}_lift_plate",
                            x, y, self.lift_z)

        # 부착된 선반 (x,y + z 업데이트)
        if self.carrying_shelf is not None:
            self._sync_shelf(stage)

    def _sync_shelf(self, stage):
        """선반 위치 동기화 (delta 방식: x,y 변위 + 리프트 z 반영)"""
        if self.carrying_shelf is None:
            return
        x, y = float(self.pos[0]), float(self.pos[1])
        orig = shelf_origins.get(self.carrying_shelf, (x, y))
        dx = x - orig[0]
        dy = y - orig[1]
        dz = max(0.0, self.lift_z - self.LIFT_CONTACT_Z)  # 리프트가 선반에 닿은 후 상승분

        shelf_root = f"/World/Shelf_{self.carrying_shelf}"
        prim = stage.GetPrimAtPath(shelf_root)
        if not prim.IsValid():
            return
        xform = UsdGeom.Xformable(prim)
        for op in xform.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(dx, dy, dz))
                return
        xform.AddTranslateOp().Set(Gf.Vec3d(dx, dy, dz))

    def _sync_lift(self, stage):
        """상판 z 업데이트 (리프트 애니메이션) + 선반 z 연동"""
        x, y = float(self.pos[0]), float(self.pos[1])
        self._set_translate(stage, f"/World/AGV_{self.rid}_lift_plate",
                            x, y, self.lift_z)
        if self.carrying_shelf is not None:
            self._sync_shelf(stage)

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
        """Z축 회전 설정 (heading 방향)"""
        prim = stage.GetPrimAtPath(path)
        if not prim.IsValid():
            return
        half = angle_rad / 2
        q = Gf.Quatd(float(np.cos(half)), Gf.Vec3d(0.0, 0.0, float(np.sin(half))))
        attr = prim.GetAttribute("xformOp:orient")
        if attr and attr.IsValid():
            attr.Set(q)
        else:
            UsdGeom.Xformable(prim).AddOrientOp().Set(q)


# ═══════════════════════════════════════════════════════════════════════════════
# MQTTBridge — MQTT 구독/발행
# ═══════════════════════════════════════════════════════════════════════════════

class MQTTBridge:
    def __init__(self, agvs: dict):
        self.agvs    = agvs
        self._client = mqtt_lib.Client()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        for agv in agvs.values():
            agv.on_arrived      = self._on_arrived
            agv.on_intermediate = self._on_intermediate
            agv.on_lift_done    = self._on_lift_done

    def connect(self):
        try:
            self._client.connect(MQTT_HOST, MQTT_PORT, 60)
            self._client.loop_start()
            print("[MQTT] Connecting to broker...")
        except Exception as e:
            print(f"[MQTT] Connection failed: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(TOPIC_PLAN)
        client.subscribe(TOPIC_CONTROL)
        client.subscribe(TOPIC_SHELF_CMD)
        print(f"[MQTT] Connected (rc={rc})")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return

        if msg.topic == TOPIC_PLAN:
            self._handle_plan(data)
        elif msg.topic == TOPIC_CONTROL:
            self._handle_control(data)
        elif msg.topic == TOPIC_SHELF_CMD:
            self._handle_shelf_cmd(data)

    # ─── 수신 핸들러 ──────────────────────────────────────────────────────────

    def _handle_plan(self, data):
        for r in data.get("robots", []):
            rid = int(r.get("rid", -1))
            agv = self.agvs.get(rid)
            if agv is None:
                continue
            node_path = r.get("node_path", [])
            goal      = r.get("goal")
            if node_path and len(node_path) > 1:
                start_node = node_path[0]
                if agv.state == "MOVING" and agv._moving_to_node == start_node:
                    # 이미 start_node를 향해 이동 중 → 도착 후 자연스럽게 이어지도록 경로만 교체
                    agv.path_queue = list(node_path[1:])
                    agv.goal_node  = goal
                else:
                    # IDLE 또는 NODE_WAIT → 현재 위치와 start_node가 일치하므로 스냅 안전
                    agv.pos = node_xy(start_node).copy()
                    agv.set_plan(list(node_path[1:]), goal)
                print(f"[AGV {rid}] Plan: {node_path} → goal {goal}")
            elif goal is not None:
                agv.current_node = goal
                self._publish_arrived(rid, goal)

    def _handle_control(self, data):
        rid = int(data.get("rid", -1))
        agv = self.agvs.get(rid)
        if agv and data.get("cmd") == "resume":
            agv.resume()
            print(f"[AGV {rid}] ← resume")

    def _handle_shelf_cmd(self, data):
        rid      = int(data.get("rid", -1))
        command  = data.get("command")
        shelf_id = data.get("shelf_id")
        agv = self.agvs.get(rid)
        if agv is None:
            return
        if command == "pickup":
            agv.cmd_pickup(shelf_id)
        elif command == "putdown":
            agv.cmd_putdown(shelf_id)

    # ─── AGV 콜백 → 발행 ─────────────────────────────────────────────────────

    def _on_arrived(self, rid: int, node: int):
        self._publish_arrived(rid, node)
        self._publish_marker(rid, node)

    def _on_intermediate(self, rid: int, node: int):
        self._publish_position(rid, node)
        self._publish_marker(rid, node)

    def _on_lift_done(self, rid: int, shelf_id: int, command: str):
        msg = {"rid": rid, "command": command, "shelf_id": shelf_id, "status": "done"}
        self._client.publish(TOPIC_SHELF_ACK, json.dumps(msg))
        print(f"[AGV {rid}] → /agv/shelf_ack  {command} shelf {shelf_id}")

    # ─── 발행 ─────────────────────────────────────────────────────────────────

    def _publish_arrived(self, rid: int, node: int):
        msg = {"type": "robot_arrived", "rid": rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))
        print(f"[AGV {rid}] → /agv/arrived  node={node}")

    def _publish_position(self, rid: int, node: int):
        msg = {"type": "robot_position", "rid": rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))

    def _publish_marker(self, rid: int, node: int):
        msg = {"rid": rid, "marker_id": node, "ts": int(time.time())}
        self._client.publish(TOPIC_MARKER, json.dumps(msg))

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()


# ═══════════════════════════════════════════════════════════════════════════════
# 씬 구성 (Step 3와 동일)
# ═══════════════════════════════════════════════════════════════════════════════

C_FRAME       = np.array([0.28, 0.28, 0.30])
C_SHELF_BOARD = np.array([0.72, 0.72, 0.72])
C_WS          = np.array([0.1,  0.75, 0.1])
C_AGV1        = np.array([0.9,  0.15, 0.15])
C_AGV2        = np.array([0.15, 0.35, 1.0])
C_WHEEL       = np.array([0.15, 0.15, 0.15])
C_SCISSOR     = np.array([1.0,  0.75, 0.0])

LEG_SIZE    = 0.05
LEG_HEIGHT  = 0.85
LEG_OFFSET  = 0.35
BOARD_WIDTH = 0.76
BOARD_THK   = 0.025
BOARD_ZS    = [0.40, 0.62, 0.84]
LEG_CORNERS = [(-1, -1), (1, -1), (-1, 1), (1, 1)]

MARKER_SIZE = 0.15
MARKER_Z    = 0.002

WHEEL_QUAT     = np.array([0.7071, 0.7071, 0.0,    0.0])
SCISSOR_QUAT_A = np.array([0.9239, 0.0,    0.3827,  0.0])
SCISSOR_QUAT_B = np.array([0.9239, 0.0,   -0.3827,  0.0])


shelf_origins: dict = {}  # node_id → (orig_x, orig_y)


def build_shelf(stage, node_id: int, x: float, y: float):
    root = f"/World/Shelf_{node_id}"
    UsdGeom.Xform.Define(stage, root)  # 루트 translate 없음 (delta 방식)
    shelf_origins[node_id] = (x, y)    # 원래 위치 저장

    for i, (dx, dy) in enumerate(LEG_CORNERS):
        VisualCuboid(
            prim_path=f"{root}/col_{i}", name=f"shelf_{node_id}_col_{i}",
            position=np.array([x + dx * LEG_OFFSET, y + dy * LEG_OFFSET, LEG_HEIGHT / 2]),
            scale=np.array([LEG_SIZE, LEG_SIZE, LEG_HEIGHT]), color=C_FRAME,
        )

    for i, bz in enumerate(BOARD_ZS):
        beam_z   = bz - 0.032
        beam_len = BOARD_WIDTH - LEG_SIZE * 2
        beam_thk = 0.03

        VisualCuboid(
            prim_path=f"{root}/board_{i}", name=f"shelf_{node_id}_board_{i}",
            position=np.array([x, y, bz]),
            scale=np.array([BOARD_WIDTH, BOARD_WIDTH, BOARD_THK]), color=C_SHELF_BOARD,
        )
        for name, px, py, sx, sy in [
            ("beam_front", x, y - LEG_OFFSET, beam_len, beam_thk),
            ("beam_back",  x, y + LEG_OFFSET, beam_len, beam_thk),
            ("beam_left",  x - LEG_OFFSET, y, beam_thk, beam_len),
            ("beam_right", x + LEG_OFFSET, y, beam_thk, beam_len),
        ]:
            VisualCuboid(
                prim_path=f"{root}/{name}_{i}", name=f"shelf_{node_id}_{name}_{i}",
                position=np.array([px, py, beam_z]),
                scale=np.array([sx, sy, beam_thk]), color=C_FRAME,
            )


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
# 메인
# ═══════════════════════════════════════════════════════════════════════════════

world = World(stage_units_in_meters=1.0)
world.scene.add_default_ground_plane()
stage = omni.usd.get_context().get_stage()

# STEP A: 선반 + 작업대 + AGV (ArUco 보다 먼저)
for node_id in shelf_node_ids:
    node = nodes[node_id]
    build_shelf(stage, node_id, node["x"], node["y"])

for node_id in ws_node_ids:
    node = nodes[node_id]
    VisualCuboid(
        prim_path=f"/World/WS_{node_id}", name=f"ws_{node_id}",
        position=np.array([node["x"], node["y"], 0.025]),
        scale=np.array([0.9, 0.9, 0.05]), color=C_WS,
    )

AGV_COLORS = {1: C_AGV1, 2: C_AGV2}
agvs: dict[int, IsaacAGV] = {}
for rid, home_node in sorted(robot_homes.items()):
    n = nodes[home_node]
    x, y = n["x"], n["y"]

    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_body", name=f"agv_{rid}_body",
        position=np.array([x, y, IsaacAGV.BODY_Z]),
        scale=np.array([0.38, 0.38, 0.08]), color=AGV_COLORS[rid],
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
        orientation=SCISSOR_QUAT_A, scale=np.array([0.025, 0.08, 0.16]), color=C_SCISSOR,
    )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_b", name=f"agv_{rid}_scissor_b",
        position=np.array([x, y - 0.06, IsaacAGV.SCISSOR_Z]),
        orientation=SCISSOR_QUAT_B, scale=np.array([0.025, 0.08, 0.16]), color=C_SCISSOR,
    )
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_lift_plate", name=f"agv_{rid}_lift_plate",
        position=np.array([x, y, IsaacAGV.LIFT_PLATE_Z]),
        scale=np.array([0.36, 0.36, 0.02]), color=C_SCISSOR,
    )
    agvs[rid] = IsaacAGV(rid, home_node)
    print(f"[AGV {rid}] Home: node {home_node}  ({x}, {y})")

world.reset()
print("  선반/작업대/AGV 배치 완료, world.reset() 완료")

# STEP B: ArUco 바닥 마커 (reset 이후)
for node_id, node in nodes.items():
    create_aruco_marker(stage, node_id, node["x"], node["y"])
print(f"  ArUco 마커 배치 완료: {len(nodes)}개")

# MQTT
bridge = MQTTBridge(agvs)
bridge.connect()

set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

print()
print("=" * 60)
print("  Isaac Sim 5.1.0 — Step 4: 리프트 + 선반 이동")
print("=" * 60)
print(f"  AGV-1 홈: 노드{robot_homes[1]}  AGV-2 홈: 노드{robot_homes[2]}")
print(f"  구독: {TOPIC_PLAN}  {TOPIC_CONTROL}  {TOPIC_SHELF_CMD}")
print(f"  발행: {TOPIC_ARRIVED}  {TOPIC_MARKER}  {TOPIC_SHELF_ACK}")
print("-" * 60)
print("  창을 닫으면 종료됩니다.")
print("=" * 60)
print()

for _ in range(10):
    simulation_app.update()

# ─── 메인 루프 ────────────────────────────────────────────────────────────────
while simulation_app.is_running():
    dt = world.get_physics_dt()
    world.step(render=True)
    for agv in agvs.values():
        agv.update(dt, stage)

bridge.disconnect()
simulation_app.close()
