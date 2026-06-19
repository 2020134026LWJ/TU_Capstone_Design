"""
Step 3 — AGV 이동 + MQTT 연동

구현 내용:
  1. Step 2 환경 씬 (3층 선반 + 작업대 + ArUco 바닥 마커) 유지
  2. AGV 박스 2대 홈 노드에 배치 (빨강=AGV1, 파랑=AGV2)
  3. MQTT /agv/plan 수신 → 경로 추종 (선형 보간 이동)
  4. 노드 도착 → /agv/arrived 발행 / 중간 노드 → NODE_WAIT + resume 대기
  5. /agv/control resume 수신 → 다음 노드 이동
  6. 노드 통과 시 /agv/marker 자동 발행 (Step 5에서 카메라로 교체 예정)

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh step3_agv_mqtt.py
"""

# SimulationApp 반드시 다른 import보다 먼저 생성
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

TOPIC_PLAN    = "/agv/plan"
TOPIC_ARRIVED = "/agv/arrived"
TOPIC_CONTROL = "/agv/control"
TOPIC_MARKER  = "/agv/marker"

# ─── 이동 파라미터 ────────────────────────────────────────────────────────────
MOVE_SPEED         = 1.5    # m/s
POSITION_TOLERANCE = 0.05   # m (노드 도착 판정 거리)

# ─── 설정 파일 경로 ───────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.join(_HERE, "..")
MAP_PATH   = os.path.join(_ROOT, "server", "data", "map.json")
SHELF_PATH = os.path.join(_ROOT, "server", "data", "shelf_config.json")
ROBOT_PATH = os.path.join(_ROOT, "server", "data", "robot_config.json")
ARUCO_DIR  = os.path.join(_ROOT, "webots_simulation", "textures", "aruco_markers")

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
    """노드 ID → (x, y) 좌표"""
    n = nodes[node_id]
    return np.array([n["x"], n["y"]], dtype=float)


# ═══════════════════════════════════════════════════════════════════════════════
# IsaacAGV — AGV 이동 상태머신
# ═══════════════════════════════════════════════════════════════════════════════

class IsaacAGV:
    BODY_Z        = 0.10    # 바디 중심 높이
    WHEEL_Z       = 0.06    # 바퀴 중심 높이 (바닥 접지)
    # 바디 상면 = 0.14, 상판 = 0.25 → 높이차 0.11
    # 막대길이 = 0.11 / sin(45°) = 0.156 → 0.16 사용
    # 막대 중심 = 0.14 + 0.11/2 = 0.195
    SCISSOR_Z     = 0.195   # 시저리프트 막대 중심 높이
    LIFT_PLATE_Z  = 0.25    # 시저리프트 상단 플레이트 높이 (선반 1층 0.27보다 낮게)
    WHEEL_OFFSETS = [       # 바퀴 2개 (좌/우)
        (0.0, +0.22),       # 왼쪽
        (0.0, -0.22),       # 오른쪽
    ]

    def __init__(self, rid: int, home_node: int):
        self.rid          = rid
        self.current_node = home_node

        # 상태: IDLE / MOVING / NODE_WAIT
        self.state      = "IDLE"
        self.path_queue = []
        self.goal_node  = None

        home = node_xy(home_node)
        self.pos             = home.copy()
        self.target_pos      = home.copy()
        self._moving_to_node = home_node

        # 콜백 (MQTTBridge가 등록)
        self.on_arrived      = None   # fn(rid, node)
        self.on_intermediate = None   # fn(rid, node)

    # ─── 경로 설정 ────────────────────────────────────────────────────────────

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
        """서버로부터 다음 노드 이동 허가 수신"""
        if self.state == "NODE_WAIT" and self.path_queue:
            self._move_to_next()

    # ─── 업데이트 (매 world.step()) ──────────────────────────────────────────

    def update(self, dt: float, stage):
        if self.state != "MOVING":
            return

        diff = self.target_pos - self.pos
        dist = float(np.linalg.norm(diff))

        if dist < POSITION_TOLERANCE:
            # 노드 도착
            self.pos          = self.target_pos.copy()
            self.current_node = self._moving_to_node
            self._sync_prim(stage)
            print(f"[AGV {self.rid}] Reached node {self.current_node}")

            if self.path_queue:
                # 중간 노드 → NODE_WAIT (서버 resume 대기)
                self.state = "NODE_WAIT"
                if self.on_intermediate:
                    self.on_intermediate(self.rid, self.current_node)
            else:
                # 최종 목표 도착
                self.state     = "IDLE"
                self.goal_node = None
                if self.on_arrived:
                    self.on_arrived(self.rid, self.current_node)
        else:
            # 선형 보간 이동
            step = min(MOVE_SPEED * dt, dist)
            self.pos += (diff / dist) * step
            self._sync_prim(stage)

    def _sync_prim(self, stage):
        """USD prim 위치 동기화 (바디 + 바퀴 2개 + 시저리프트)"""
        x, y = float(self.pos[0]), float(self.pos[1])

        # 바디
        body = stage.GetPrimAtPath(f"/World/AGV_{self.rid}_body")
        if body.IsValid():
            attr = body.GetAttribute("xformOp:translate")
            if attr:
                attr.Set(Gf.Vec3d(x, y, self.BODY_Z))

        # 바퀴 2개
        for i, (dx, dy) in enumerate(self.WHEEL_OFFSETS):
            wheel = stage.GetPrimAtPath(f"/World/AGV_{self.rid}_wheel_{i}")
            if wheel.IsValid():
                attr = wheel.GetAttribute("xformOp:translate")
                if attr:
                    attr.Set(Gf.Vec3d(x + dx, y + dy, self.WHEEL_Z))

        # 시저리프트 막대 a, b
        for name, z in [("scissor_a", self.SCISSOR_Z), ("scissor_b", self.SCISSOR_Z),
                        ("lift_plate", self.LIFT_PLATE_Z)]:
            prim = stage.GetPrimAtPath(f"/World/AGV_{self.rid}_{name}")
            if prim.IsValid():
                attr = prim.GetAttribute("xformOp:translate")
                if attr:
                    attr.Set(Gf.Vec3d(x, y, z))


# ═══════════════════════════════════════════════════════════════════════════════
# MQTTBridge — MQTT 구독/발행 (paho 별도 스레드)
# ═══════════════════════════════════════════════════════════════════════════════

class MQTTBridge:
    def __init__(self, agvs: dict):
        self.agvs    = agvs   # {rid: IsaacAGV}
        self._client = mqtt_lib.Client()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # AGV 콜백 연결
        for agv in agvs.values():
            agv.on_arrived      = self._on_arrived
            agv.on_intermediate = self._on_intermediate

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
        print(f"[MQTT] Connected (rc={rc})")
        print(f"[MQTT] Subscribed: {TOPIC_PLAN}, {TOPIC_CONTROL}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic == TOPIC_PLAN:
            self._handle_plan(data)
        elif msg.topic == TOPIC_CONTROL:
            self._handle_control(data)

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
                agv.set_plan(list(node_path[1:]), goal)
                print(f"[AGV {rid}] Plan received: {node_path} → goal {goal}")
            elif goal is not None:
                agv.current_node = goal
                self._publish_arrived(rid, goal)
                print(f"[AGV {rid}] Already at goal {goal}")

    def _handle_control(self, data):
        rid = int(data.get("rid", -1))
        cmd = data.get("cmd")
        agv = self.agvs.get(rid)
        if agv and cmd == "resume":
            agv.resume()
            print(f"[AGV {rid}] ← resume")

    # ─── AGV 콜백 (이동 결과 → MQTT 발행) ────────────────────────────────────

    def _on_arrived(self, rid: int, node: int):
        self._publish_arrived(rid, node)
        self._publish_marker(rid, node)

    def _on_intermediate(self, rid: int, node: int):
        self._publish_position(rid, node)
        self._publish_marker(rid, node)

    # ─── 발행 ─────────────────────────────────────────────────────────────────

    def _publish_arrived(self, rid: int, node: int):
        msg = {"type": "robot_arrived", "rid": rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))
        print(f"[AGV {rid}] → /agv/arrived  node={node}")

    def _publish_position(self, rid: int, node: int):
        """중간 노드 통과 알림 (서버 충돌 회피용)"""
        msg = {"type": "robot_position", "rid": rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))
        print(f"[AGV {rid}] → /agv/arrived (position)  node={node}")

    def _publish_marker(self, rid: int, node: int):
        """노드 통과 시 마커 자동 발행 (Step 5: 카메라 인식으로 교체 예정)"""
        msg = {"rid": rid, "marker_id": node, "ts": int(time.time())}
        self._client.publish(TOPIC_MARKER, json.dumps(msg))

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()


# ═══════════════════════════════════════════════════════════════════════════════
# 씬 구성 헬퍼 (Step 2와 동일)
# ═══════════════════════════════════════════════════════════════════════════════

C_FRAME       = np.array([0.28, 0.28, 0.30])  # 철제 프레임 (어두운 회색)
C_SHELF_BOARD = np.array([0.72, 0.72, 0.72])  # 선반 판 (밝은 회색)
C_WS      = np.array([0.1, 0.75, 0.1])
C_AGV1    = np.array([0.9, 0.15, 0.15])
C_AGV2    = np.array([0.15, 0.35, 1.0])
C_WHEEL   = np.array([0.15, 0.15, 0.15])  # 어두운 회색
C_SCISSOR = np.array([1.0,  0.75, 0.0])   # 노란색 (시저리프트)

# 바퀴: x축 90도 회전 [w, x, y, z]
WHEEL_QUAT = np.array([0.7071, 0.7071, 0.0, 0.0])

# 시저리프트 막대: y축 ±45도 회전
SCISSOR_QUAT_A = np.array([0.9239, 0.0,  0.3827, 0.0])  # y축 +45도
SCISSOR_QUAT_B = np.array([0.9239, 0.0, -0.3827, 0.0])  # y축 -45도

LEG_SIZE    = 0.05          # 기둥 단면 (사각)
LEG_HEIGHT  = 0.85          # 기둥 전체 높이 (최상층 0.80 + 여유)
LEG_OFFSET  = 0.35          # 기둥 중심 오프셋
BOARD_WIDTH = 0.76          # 선반 판 폭
BOARD_THK   = 0.025         # 선반 판 두께
BOARD_ZS    = [0.40, 0.62, 0.84]   # 층별 판 높이
LEG_CORNERS = [(-1, -1), (1, -1), (-1, 1), (1, 1)]

MARKER_SIZE = 0.15
MARKER_Z    = 0.002


def build_shelf(stage, node_id: int, x: float, y: float):
    root = f"/World/Shelf_{node_id}"
    UsdGeom.Xform.Define(stage, root)

    # 수직 기둥 4개 (사각형)
    for i, (dx, dy) in enumerate(LEG_CORNERS):
        VisualCuboid(
            prim_path=f"{root}/col_{i}",
            name=f"shelf_{node_id}_col_{i}",
            position=np.array([x + dx * LEG_OFFSET, y + dy * LEG_OFFSET, LEG_HEIGHT / 2]),
            scale=np.array([LEG_SIZE, LEG_SIZE, LEG_HEIGHT]),
            color=C_FRAME,
        )

    # 각 층: 선반 판 + 전면 빔 + 후면 빔 + 좌측 빔 + 우측 빔
    for i, bz in enumerate(BOARD_ZS):
        beam_z   = bz - 0.032          # 빔은 판 바로 아래
        beam_len = BOARD_WIDTH - LEG_SIZE * 2  # 기둥 사이 길이
        beam_thk = 0.03

        # 선반 판
        VisualCuboid(
            prim_path=f"{root}/board_{i}",
            name=f"shelf_{node_id}_board_{i}",
            position=np.array([x, y, bz]),
            scale=np.array([BOARD_WIDTH, BOARD_WIDTH, BOARD_THK]),
            color=C_SHELF_BOARD,
        )
        # 전면 빔 (x방향, 앞쪽 y)
        VisualCuboid(
            prim_path=f"{root}/beam_front_{i}",
            name=f"shelf_{node_id}_beam_front_{i}",
            position=np.array([x, y - LEG_OFFSET, beam_z]),
            scale=np.array([beam_len, beam_thk, beam_thk]),
            color=C_FRAME,
        )
        # 후면 빔 (x방향, 뒤쪽 y)
        VisualCuboid(
            prim_path=f"{root}/beam_back_{i}",
            name=f"shelf_{node_id}_beam_back_{i}",
            position=np.array([x, y + LEG_OFFSET, beam_z]),
            scale=np.array([beam_len, beam_thk, beam_thk]),
            color=C_FRAME,
        )
        # 좌측 빔 (y방향)
        VisualCuboid(
            prim_path=f"{root}/beam_left_{i}",
            name=f"shelf_{node_id}_beam_left_{i}",
            position=np.array([x - LEG_OFFSET, y, beam_z]),
            scale=np.array([beam_thk, beam_len, beam_thk]),
            color=C_FRAME,
        )
        # 우측 빔 (y방향)
        VisualCuboid(
            prim_path=f"{root}/beam_right_{i}",
            name=f"shelf_{node_id}_beam_right_{i}",
            position=np.array([x + LEG_OFFSET, y, beam_z]),
            scale=np.array([beam_thk, beam_len, beam_thk]),
            color=C_FRAME,
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

# STEP A: 선반 + 작업대 + AGV 박스 (ArUco 보다 먼저)
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

    # 바디
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_body",
        name=f"agv_{rid}_body",
        position=np.array([x, y, IsaacAGV.BODY_Z]),
        scale=np.array([0.38, 0.38, 0.08]),
        color=AGV_COLORS[rid],
    )

    # 바퀴 2개 (좌/우)
    for i, (dx, dy) in enumerate(IsaacAGV.WHEEL_OFFSETS):
        VisualCylinder(
            prim_path=f"/World/AGV_{rid}_wheel_{i}",
            name=f"agv_{rid}_wheel_{i}",
            position=np.array([x + dx, y + dy, IsaacAGV.WHEEL_Z]),
            orientation=WHEEL_QUAT,
            radius=0.06,
            height=0.05,
            color=C_WHEEL,
        )

    # 시저리프트 막대 a (y축 +45도)
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_a",
        name=f"agv_{rid}_scissor_a",
        position=np.array([x, y + 0.06, IsaacAGV.SCISSOR_Z]),
        orientation=SCISSOR_QUAT_A,
        scale=np.array([0.025, 0.08, 0.16]),
        color=C_SCISSOR,
    )

    # 시저리프트 막대 b (y축 -45도)
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_scissor_b",
        name=f"agv_{rid}_scissor_b",
        position=np.array([x, y - 0.06, IsaacAGV.SCISSOR_Z]),
        orientation=SCISSOR_QUAT_B,
        scale=np.array([0.025, 0.08, 0.16]),
        color=C_SCISSOR,
    )

    # 시저리프트 상단 플레이트
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_lift_plate",
        name=f"agv_{rid}_lift_plate",
        position=np.array([x, y, IsaacAGV.LIFT_PLATE_Z]),
        scale=np.array([0.36, 0.36, 0.02]),
        color=C_SCISSOR,
    )

    agvs[rid] = IsaacAGV(rid, home_node)
    print(f"[AGV {rid}] Home: node {home_node}  ({x}, {y})")

# world.reset()
world.reset()
print("  선반/작업대/AGV 배치 완료, world.reset() 완료")

# STEP B: ArUco 바닥 마커 (reset 이후)
for node_id, node in nodes.items():
    create_aruco_marker(stage, node_id, node["x"], node["y"])
print(f"  ArUco 마커 배치 완료: {len(nodes)}개")

# MQTT 연결
bridge = MQTTBridge(agvs)
bridge.connect()

# 카메라
set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

print()
print("=" * 60)
print("  Isaac Sim 5.1.0 — Step 3: AGV 이동 + MQTT 연동")
print("=" * 60)
print(f"  AGV-1 홈: 노드{robot_homes[1]}  AGV-2 홈: 노드{robot_homes[2]}")
print(f"  MQTT 브로커: {MQTT_HOST}:{MQTT_PORT}")
print(f"  구독: {TOPIC_PLAN}  {TOPIC_CONTROL}")
print(f"  발행: {TOPIC_ARRIVED}  {TOPIC_MARKER}")
print("-" * 60)
print("  창을 닫으면 종료됩니다.")
print("=" * 60)
print()

# 렌더러 워밍업
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
