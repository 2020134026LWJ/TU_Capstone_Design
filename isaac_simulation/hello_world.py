"""
Step 1 — Isaac Sim 5.1.0 환경 확인 + 8×4 창고 씬 레이아웃

기존 server/, config/ 는 건드리지 않음.
이 스크립트만 새로 추가.

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh hello_world.py
"""

# SimulationApp은 반드시 다른 import보다 먼저 생성해야 함
from isaacsim import SimulationApp
simulation_app = SimulationApp({"headless": False})

import json
import os

import numpy as np
import omni.usd
from isaacsim.core.api import World
from isaacsim.core.api.objects import VisualCuboid
from isaacsim.core.utils.viewports import set_camera_view

# ─── 설정 파일 경로 (기존 webots_simulation/config 그대로 사용) ───────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_PROJ      = os.path.join(_HERE, "..", "webots_simulation")
MAP_PATH   = os.path.join(_PROJ, "config", "map.json")
SHELF_PATH = os.path.join(_PROJ, "config", "shelf_config.json")
ROBOT_PATH = os.path.join(_PROJ, "config", "robot_config.json")

with open(MAP_PATH)   as f: map_cfg   = json.load(f)
with open(SHELF_PATH) as f: shelf_cfg = json.load(f)
with open(ROBOT_PATH) as f: robot_cfg = json.load(f)

# ─── 노드 테이블 ─────────────────────────────────────────────────────────────
nodes          = {n["id"]: n for n in map_cfg["nodes"]}
shelf_node_ids = set(map_cfg["shelf_nodes"])        # {11,12,14,15,19,20,22,23}
ws_node_ids    = set(map_cfg["workstation_nodes"])  # {33, 34}
robot_homes    = {int(rid): info["home_node"]
                  for rid, info in robot_cfg["robots"].items()}  # {1:33, 2:34}

staging_nodes = {int(ws["staging_node"]) for ws in shelf_cfg["workstations"].values()}
trigger_nodes = {int(ws["trigger_node"]) for ws in shelf_cfg["workstations"].values()}

def node_xyz(node_id, z=0.0):
    """노드 ID → Isaac Sim 3D 좌표 (map.json x/y 그대로 사용)"""
    n = nodes[node_id]
    return np.array([n["x"], n["y"], z])

# ─── World + 바닥 (add_default_ground_plane이 조명까지 포함) ─────────────────
world = World(stage_units_in_meters=1.0)
world.scene.add_default_ground_plane()

# ─── 색상 팔레트 (0.0 ~ 1.0) ─────────────────────────────────────────────────
C_SHELF   = np.array([1.0, 0.85, 0.0])   # 노란색  — 선반
C_WS      = np.array([0.1, 0.75, 0.1])   # 초록색  — 작업대
C_STAGE   = np.array([0.9, 0.55, 0.0])   # 주황색  — 스테이징 노드
C_TRIG    = np.array([0.7, 0.1,  0.9])   # 보라색  — 트리거 노드
C_NODE    = np.array([0.5, 0.5,  0.5])   # 회색    — 일반 통로
C_AGV1    = np.array([0.9, 0.15, 0.15])  # 빨간색  — AGV-1
C_AGV2    = np.array([0.15, 0.35, 1.0])  # 파란색  — AGV-2

# ─── 노드별 오브젝트 배치 ─────────────────────────────────────────────────────
for node_id, node in nodes.items():
    xy = np.array([node["x"], node["y"]])

    if node_id in shelf_node_ids:
        # 선반 — 노란 박스 (0.7×0.7, 높이 0.4m)
        VisualCuboid(
            prim_path=f"/World/Shelf_{node_id}",
            name=f"shelf_{node_id}",
            position=np.array([*xy, 0.2]),      # z = 높이/2 (바닥 위에 놓임)
            scale=np.array([0.7, 0.7, 0.4]),
            color=C_SHELF,
        )

    elif node_id in ws_node_ids:
        # 작업대 — 초록 박스 (0.9×0.9, 높이 0.3m)
        VisualCuboid(
            prim_path=f"/World/WS_{node_id}",
            name=f"ws_{node_id}",
            position=np.array([*xy, 0.15]),
            scale=np.array([0.9, 0.9, 0.3]),
            color=C_WS,
        )

    elif node_id in staging_nodes:
        # 스테이징 노드 — 주황 얇은 마커
        VisualCuboid(
            prim_path=f"/World/Staging_{node_id}",
            name=f"staging_{node_id}",
            position=np.array([*xy, 0.02]),
            scale=np.array([0.65, 0.65, 0.04]),
            color=C_STAGE,
        )

    elif node_id in trigger_nodes:
        # 트리거 노드 — 보라 얇은 마커
        VisualCuboid(
            prim_path=f"/World/Trigger_{node_id}",
            name=f"trigger_{node_id}",
            position=np.array([*xy, 0.02]),
            scale=np.array([0.65, 0.65, 0.04]),
            color=C_TRIG,
        )

    else:
        # 일반 통로 노드 — 회색 얇은 마커
        VisualCuboid(
            prim_path=f"/World/Node_{node_id}",
            name=f"node_{node_id}",
            position=np.array([*xy, 0.01]),
            scale=np.array([0.35, 0.35, 0.02]),
            color=C_NODE,
        )

# AGV 임시 박스 (향후 실제 로봇 USD로 교체)
AGV_COLORS = {1: C_AGV1, 2: C_AGV2}
for rid, home_node in sorted(robot_homes.items()):
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}",
        name=f"agv_{rid}",
        position=node_xyz(home_node, z=0.075),
        scale=np.array([0.4, 0.4, 0.15]),
        color=AGV_COLORS[rid],
    )

# ─── 출력 ─────────────────────────────────────────────────────────────────────
cols = map_cfg["grid_size"]["cols"]
rows = map_cfg["grid_size"]["rows"]

print()
print("=" * 56)
print("  Isaac Sim 5.1.0 — AGV 창고 씬 로드 완료 (Step 1)")
print("=" * 56)
print(f"  그리드   : {cols}×{rows}  ({len(nodes)}노드)")
print(f"  선반     : {sorted(shelf_node_ids)}  ({len(shelf_node_ids)}개, 노란색)")
print(f"  작업대   : W1=노드33, W2=노드34  (초록색)")
print(f"  스테이징 : {sorted(staging_nodes)}  (주황색)")
print(f"  트리거   : {sorted(trigger_nodes)}  (보라색)")
print(f"  AGV-1    : 노드{robot_homes[1]} 출발  (빨간색)")
print(f"  AGV-2    : 노드{robot_homes[2]} 출발  (파란색)")
print("-" * 56)
print("  창을 닫으면 종료됩니다.")
print("=" * 56)
print()

# ─── 시뮬레이션 루프 ─────────────────────────────────────────────────────────
world.reset()

# reset() 이후에 카메라 설정 (이전에 설정하면 reset이 덮어씀)
set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

# 렌더러 워밍업 (검은 화면 방지)
for _ in range(10):
    simulation_app.update()

while simulation_app.is_running():
    world.step(render=True)

simulation_app.close()
