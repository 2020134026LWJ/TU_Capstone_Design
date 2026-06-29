"""
Step 2 — 8×4 창고 씬 완성 (v3)

핵심 수정:
  - 선반/WS VisualCuboid 먼저 생성 → world.reset() → ArUco 마커 생성
    (ArUco USD 머티리얼이 VisualCuboid 컬러 시스템을 간섭하는 문제 해결)
  - USD 머티리얼 연결 API 수정 (Sdf.AssetPath, ConnectableAPI)

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh step2_environment.py
"""

# SimulationApp 반드시 다른 import보다 먼저 생성
from isaacsim import SimulationApp
simulation_app = SimulationApp({"headless": False})

import carb
# 뷰포트 라이팅을 "Default"로 고정 (Stage Lights 대신 카메라 기본 조명 사용)
carb.settings.get_settings().set("/persistent/app/viewport/camLightEnabled", True)

import json
import os

import numpy as np
from pxr import UsdGeom, UsdShade, Sdf, Vt, Gf
import omni.usd

from isaacsim.core.api import World
from isaacsim.core.api.objects import VisualCuboid, VisualCylinder
from isaacsim.core.utils.viewports import set_camera_view

# ─── 설정 파일 ────────────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.join(_HERE, "..")
MAP_PATH   = os.path.join(_ROOT, "server", "data", "map.json")
SHELF_PATH = os.path.join(_ROOT, "server", "data", "shelf_config.json")
ARUCO_DIR  = os.path.join(_ROOT, "isaac_simulation", "aruco_markers")

with open(MAP_PATH)   as f: map_cfg   = json.load(f)
with open(SHELF_PATH) as f: shelf_cfg = json.load(f)

nodes          = {n["id"]: n for n in map_cfg["nodes"]}
shelf_node_ids = set(map_cfg["shelf_nodes"])
ws_node_ids    = set(map_cfg["workstation_nodes"])
staging_nodes  = {int(ws["staging_node"]) for ws in shelf_cfg["workstations"].values()}
trigger_nodes  = {int(ws["trigger_node"]) for ws in shelf_cfg["workstations"].values()}

# ─── 색상 ────────────────────────────────────────────────────────────────────
C_LEG     = np.array([0.6, 0.6, 0.6])
C_BOARD_1 = np.array([0.2, 0.4, 0.9])   # 1층 — 파랑
C_BOARD_2 = np.array([0.2, 0.8, 0.2])   # 2층 — 초록
C_BOARD_3 = np.array([0.9, 0.2, 0.2])   # 3층 — 빨강
C_WS      = np.array([0.1, 0.75, 0.1])

# ─── World + 바닥 ─────────────────────────────────────────────────────────────
world = World(stage_units_in_meters=1.0)
world.scene.add_default_ground_plane()
stage = omni.usd.get_context().get_stage()

# ═══════════════════════════════════════════════════════════════════════════════
# STEP A: 선반 + 작업대 (VisualCuboid/VisualCylinder) ← ArUco 보다 먼저
# ═══════════════════════════════════════════════════════════════════════════════

LEG_RADIUS  = 0.03
LEG_HEIGHT  = 0.28
LEG_OFFSET  = 0.35
BOARD_WIDTH = 0.78
BOARD_THK   = 0.02
BOARDS      = [(0.28, C_BOARD_1), (0.43, C_BOARD_2), (0.58, C_BOARD_3)]
LEG_CORNERS = [(-1, -1), (1, -1), (-1, 1), (1, 1)]


def build_shelf(stage, node_id: int, x: float, y: float):
    """3층 선반: 4 원기둥 다리 + 3 직육면체 판"""
    root = f"/World/Shelf_{node_id}"
    UsdGeom.Xform.Define(stage, root)

    for i, (dx, dy) in enumerate(LEG_CORNERS):
        VisualCylinder(
            prim_path=f"{root}/leg_{i}",
            name=f"shelf_{node_id}_leg_{i}",
            position=np.array([x + dx * LEG_OFFSET,
                                y + dy * LEG_OFFSET,
                                LEG_HEIGHT / 2]),
            radius=LEG_RADIUS,
            height=LEG_HEIGHT,
            color=C_LEG,
        )

    for i, (z, color) in enumerate(BOARDS):
        VisualCuboid(
            prim_path=f"{root}/board_{i}",
            name=f"shelf_{node_id}_board_{i}",
            position=np.array([x, y, z]),
            scale=np.array([BOARD_WIDTH, BOARD_WIDTH, BOARD_THK]),
            color=color,
        )


for node_id in shelf_node_ids:
    node = nodes[node_id]
    build_shelf(stage, node_id, node["x"], node["y"])

for node_id in ws_node_ids:
    node = nodes[node_id]
    VisualCuboid(
        prim_path=f"/World/WS_{node_id}",
        name=f"ws_{node_id}",
        position=np.array([node["x"], node["y"], 0.025]),
        scale=np.array([0.9, 0.9, 0.05]),
        color=C_WS,
    )

# ─── world.reset() ─────────────────────────────────────────────────────────────
world.reset()
print("  선반/작업대 배치 완료, world.reset() 완료")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP B: ArUco 바닥 마커 (world.reset() 이후에 USD API로 생성)
# ═══════════════════════════════════════════════════════════════════════════════

MARKER_SIZE = 0.15
MARKER_Z    = 0.002


def _make_aruco_material(stage, node_id: int) -> UsdShade.Material:
    """UsdPreviewSurface + UsdUVTexture 머티리얼"""
    mat_path = f"/World/ArUcoMats/mat_{node_id}"
    material  = UsdShade.Material.Define(stage, mat_path)

    shader = UsdShade.Shader.Define(stage, f"{mat_path}/shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(1.0)
    shader.CreateInput("metallic",  Sdf.ValueTypeNames.Float).Set(0.0)

    # 텍스처 파일
    tex = UsdShade.Shader.Define(stage, f"{mat_path}/tex")
    tex.CreateIdAttr("UsdUVTexture")
    png_path = os.path.join(ARUCO_DIR, f"aruco_{node_id}.png")
    tex.CreateInput("file",  Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(png_path))
    tex.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("clamp")
    tex.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("clamp")

    # st primvar reader
    st_reader = UsdShade.Shader.Define(stage, f"{mat_path}/stReader")
    st_reader.CreateIdAttr("UsdPrimvarReader_float2")
    st_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")

    # 연결: stReader.result → tex.st
    tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
        st_reader.ConnectableAPI(), "result", UsdShade.AttributeType.Output
    )
    # 연결: tex.rgb → shader.diffuseColor
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        tex.ConnectableAPI(), "rgb", UsdShade.AttributeType.Output
    )
    # 연결: shader.surface → material surface output
    material.CreateSurfaceOutput().ConnectToSource(
        shader.ConnectableAPI(), "surface", UsdShade.AttributeType.Output
    )
    return material


def create_aruco_marker(stage, node_id: int, x: float, y: float):
    """UV-매핑 평면 메시 + ArUco 텍스처 바인딩"""
    prim_path = f"/World/Marker_{node_id}"
    h = MARKER_SIZE / 2

    mesh = UsdGeom.Mesh.Define(stage, prim_path)
    mesh.CreatePointsAttr(Vt.Vec3fArray([
        Gf.Vec3f(x - h, y - h, MARKER_Z),
        Gf.Vec3f(x + h, y - h, MARKER_Z),
        Gf.Vec3f(x + h, y + h, MARKER_Z),
        Gf.Vec3f(x - h, y + h, MARKER_Z),
    ]))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([4]))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray([0, 1, 2, 3]))
    mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 1)] * 4))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)

    # UV (st) primvar
    st_primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
    )
    st_primvar.Set(Vt.Vec2fArray([
        Gf.Vec2f(0, 0),
        Gf.Vec2f(1, 0),
        Gf.Vec2f(1, 1),
        Gf.Vec2f(0, 1),
    ]))

    material = _make_aruco_material(stage, node_id)
    UsdShade.MaterialBindingAPI(mesh.GetPrim()).Bind(material)


for node_id, node in nodes.items():
    create_aruco_marker(stage, node_id, node["x"], node["y"])

print(f"  ArUco 마커 배치 완료: {len(nodes)}개")

# ─── 카메라 ────────────────────────────────────────────────────────────────────
set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

# ─── 출력 ─────────────────────────────────────────────────────────────────────
cols = map_cfg["grid_size"]["cols"]
rows = map_cfg["grid_size"]["rows"]

print()
print("=" * 60)
print("  Isaac Sim 5.1.0 — Step 2 v3: 창고 환경 씬")
print("=" * 60)
print(f"  그리드    : {cols}×{rows}  ({len(nodes)} 노드)")
print(f"  ArUco 마커: {len(nodes)}개  (Webots PNG 재사용)")
print(f"  3층 선반  : {sorted(shelf_node_ids)}  ({len(shelf_node_ids)}개)")
print(f"  작업대    : W1=노드33, W2=노드34")
print("-" * 60)
print("  창을 닫으면 종료됩니다.")
print("=" * 60)
print()

for _ in range(10):
    simulation_app.update()

while simulation_app.is_running():
    world.step(render=True)

simulation_app.close()
