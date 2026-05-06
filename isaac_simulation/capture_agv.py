"""
capture_agv.py — AGV 선반 운반 포즈 촬영용 스크립트

MQTT / 이동 / 서버 연결 없음. AGV + 선반 정적 포즈만 표시.
S 키: 스크린샷 저장 (실행 디렉터리에 agv_capture.png)
P 키: AGV 회전 (45° 씩 돌려가며 촬영)
실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh \\
        /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/capture_agv.py
"""

from isaacsim import SimulationApp
simulation_app = SimulationApp({"headless": False, "width": 1920, "height": 1080})

import carb
import carb.input
import numpy as np
import os

from pxr import UsdGeom, UsdLux, Gf
import omni.usd
import omni.appwindow

from isaacsim.core.api import World
from isaacsim.core.api.objects import VisualCuboid, VisualCylinder, VisualSphere
from isaacsim.core.utils.viewports import set_camera_view

# ─── 색상 ────────────────────────────────────────────────────────────────────
C_FRAME       = np.array([0.20, 0.20, 0.22])
C_SHELF_BOARD = np.array([0.82, 0.74, 0.60])
C_AGV         = np.array([0.95, 0.32, 0.08])   # 주황 (AGV-1 색상)
C_WHEEL       = np.array([0.10, 0.10, 0.10])
C_SCISSOR     = np.array([0.85, 0.68, 0.05])
C_LED         = np.array([1.0,  0.50, 0.0])
C_FLOOR       = np.array([0.30, 0.30, 0.32])
C_TRAY        = np.array([0.65, 0.65, 0.70])
C_TABLE_LEG   = np.array([0.58, 0.60, 0.63])
C_WORKER_SUIT = np.array([0.98, 0.80, 0.10])
C_WORKER_HEAD = np.array([0.92, 0.72, 0.56])
C_BOX = [
    np.array([0.72, 0.52, 0.04]),
    np.array([0.20, 0.45, 0.72]),
    np.array([0.85, 0.15, 0.10]),
    np.array([0.15, 0.65, 0.15]),
    np.array([0.70, 0.70, 0.70]),
]

# ─── AGV 치수 (step6_visual.py 와 동일) ──────────────────────────────────────
BODY_Z          = 0.10
WHEEL_Z         = 0.06
SCISSOR_Z       = 0.195
LIFT_PLATE_UP   = 0.42      # 리프트 올라간 상태 Z
LIFT_CONTACT_Z  = 0.3775
WHEEL_BASE      = 0.44
WHEEL_RADIUS    = 0.06
WHEEL_OFFSETS   = [(0.0, +0.22), (0.0, -0.22)]

# ─── 선반 치수 ────────────────────────────────────────────────────────────────
LEG_SIZE    = 0.05
LEG_HEIGHT  = 0.85
LEG_OFFSET  = 0.35
BOARD_WIDTH = 0.76
BOARD_THK   = 0.025
BOARD_ZS    = [0.40, 0.62, 0.84]
LEG_CORNERS = [(-1, -1), (1, -1), (-1, 1), (1, 1)]


# ─── 작업대 / 작업자 빌드 함수 ────────────────────────────────────────────────

def build_workstation(stage, ws_id: int, x: float, y: float):
    """컨베이어 작업대 — AGV(x, y) 기준 +Y 방향에 배치"""
    root = f"/World/WS_{ws_id}"
    UsdGeom.Xform.Define(stage, root)

    CONV_H  = 0.80
    CONV_LX = 1.30
    CONV_W  = 0.42
    # 컨베이어는 AGV 옆(+Y)에서 -X 방향으로 뻗도록 배치
    # x축: 선반 앞쪽(x+0.5)에서 시작해 오른쪽으로
    CONV_CX = x - 1.10
    CONV_CY = y

    # 양쪽 프레임 빔
    for si, sy_off in enumerate([-1, 1]):
        VisualCuboid(
            prim_path=f"{root}/side_{si}", name=f"ws_{ws_id}_side_{si}",
            position=np.array([CONV_CX, CONV_CY + sy_off * CONV_W / 2, CONV_H]),
            scale=np.array([CONV_LX, 0.04, 0.07]), color=C_TABLE_LEG,
        )
    # 다리 4개
    for li, (ldx, ldy) in enumerate([(-1, -1), (1, -1), (-1, 1), (1, 1)]):
        VisualCylinder(
            prim_path=f"{root}/leg_{li}", name=f"ws_{ws_id}_leg_{li}",
            position=np.array([CONV_CX + ldx * (CONV_LX / 2 - 0.06),
                               CONV_CY + ldy * (CONV_W  / 2 - 0.04),
                               (CONV_H - 0.03) / 2]),
            radius=0.025, height=CONV_H - 0.03, color=C_TABLE_LEG,
        )
    # 롤러 8개
    ROLLER_Q = np.array([0.7071, 0.7071, 0.0, 0.0])
    for ri in range(8):
        rx = CONV_CX - CONV_LX / 2 + 0.08 + ri * (CONV_LX - 0.16) / 7
        VisualCylinder(
            prim_path=f"{root}/roller_{ri}", name=f"ws_{ws_id}_roller_{ri}",
            position=np.array([rx, CONV_CY, CONV_H + 0.022]),
            orientation=ROLLER_Q,
            radius=0.022, height=CONV_W - 0.06,
            color=np.array([0.75, 0.75, 0.78]),
        )
    # 수납 박스
    bin_x = CONV_CX - CONV_LX / 2 - 0.28
    VisualCuboid(
        prim_path=f"{root}/out_bin", name=f"ws_{ws_id}_out_bin",
        position=np.array([bin_x, CONV_CY, 0.25]),
        scale=np.array([0.44, 0.58, 0.46]), color=np.array([0.30, 0.30, 0.33]),
    )
    VisualCuboid(
        prim_path=f"{root}/out_items", name=f"ws_{ws_id}_out_items",
        position=np.array([bin_x, CONV_CY, 0.50]),
        scale=np.array([0.38, 0.50, 0.06]), color=np.array([0.85, 0.75, 0.20]),
    )
    # 상태 LED
    VisualCylinder(
        prim_path=f"{root}/status_led", name=f"ws_{ws_id}_led",
        position=np.array([CONV_CX + CONV_LX / 2 - 0.1, CONV_CY + CONV_W / 2, CONV_H + 0.10]),
        radius=0.022, height=0.05, color=np.array([0.1, 0.9, 0.1]),
    )


def build_worker(stage, ws_id: int, x: float, y: float):
    """작업자 프록시 — 실린더 몸통 + 구 머리"""
    root = f"/World/Worker_{ws_id}"
    UsdGeom.Xform.Define(stage, root)
    wx = x - 1.10
    wy = y + 0.55
    VisualCylinder(
        prim_path=f"{root}/body", name=f"worker_{ws_id}_body",
        position=np.array([wx, wy, 0.45]),
        radius=0.12, height=0.90, color=C_WORKER_SUIT,
    )
    VisualSphere(
        prim_path=f"{root}/head", name=f"worker_{ws_id}_head",
        position=np.array([wx, wy, 0.97]),
        radius=0.115, color=C_WORKER_HEAD,
    )
    # 헬멧
    VisualCylinder(
        prim_path=f"{root}/helmet", name=f"worker_{ws_id}_helmet",
        position=np.array([wx, wy, 1.01]),
        radius=0.13, height=0.06, color=np.array([0.95, 0.85, 0.05]),
    )


# ─── 씬 구성 ─────────────────────────────────────────────────────────────────

world = World(physics_dt=1/60, stage_units_in_meters=1.0)
stage = omni.usd.get_context().get_stage()

# ─── 흰색 배경 ───────────────────────────────────────────────────────────────
# 돔 라이트 (흰색) → 배경 + 전체 조명
dome = UsdLux.DomeLight.Define(stage, "/World/DomeLight")
dome.CreateIntensityAttr(2000.0)
dome.CreateColorAttr(Gf.Vec3f(1.0, 1.0, 1.0))

# RTX 배경색 흰색으로
carb.settings.get_settings().set("/rtx/pathtracing/backgroundColor", [1.0, 1.0, 1.0])
carb.settings.get_settings().set("/rtx/sceneDb/ambientLightColor",   [1.0, 1.0, 1.0])

# 바닥 (작업대 포함 영역으로 확장)
VisualCuboid(
    prim_path="/World/Floor", name="floor",
    position=np.array([-1.0, 0.5, -0.01]),
    scale=np.array([6.0, 5.0, 0.02]), color=C_FLOOR,
)

# ─── AGV 빌드 (X=0, Y=0, heading=0 = East) ───────────────────────────────────
AGV_X, AGV_Y = 0.0, 0.0
HEADING = 0.0   # East (0 rad): 변경하려면 이 값 수정

cos_h = float(np.cos(HEADING))
sin_h = float(np.sin(HEADING))

# 동체
VisualCuboid(
    prim_path="/World/AGV_body", name="agv_body",
    position=np.array([AGV_X, AGV_Y, BODY_Z]),
    scale=np.array([0.38, 0.38, 0.08]), color=C_AGV,
)
# LED
VisualCylinder(
    prim_path="/World/AGV_led", name="agv_led",
    position=np.array([AGV_X, AGV_Y, 0.155]),
    radius=0.025, height=0.03, color=C_LED,
)
# 바퀴 (heading 기준 좌우)
WHEEL_QUAT = np.array([0.7071, 0.7071, 0.0, 0.0])
for i, (wdx, wdy) in enumerate(WHEEL_OFFSETS):
    rx = wdx * cos_h - wdy * sin_h
    ry = wdx * sin_h + wdy * cos_h
    VisualCylinder(
        prim_path=f"/World/AGV_wheel_{i}", name=f"agv_wheel_{i}",
        position=np.array([AGV_X + rx, AGV_Y + ry, WHEEL_Z]),
        orientation=WHEEL_QUAT, radius=WHEEL_RADIUS, height=0.05, color=C_WHEEL,
    )

# 시저리프트 — 올라간 상태로 계산
def _scissor_state(lift_z: float):
    body_top = BODY_Z + 0.04
    lift_bot = lift_z - 0.01
    h_gap = lift_bot - body_top
    arm_half = 0.14
    tilt = float(np.arccos(max(-1.0, min(1.0, h_gap / (2 * arm_half)))))
    sc_z = body_top + arm_half * float(np.cos(tilt))
    return sc_z, tilt

sc_z, sc_tilt = _scissor_state(LIFT_PLATE_UP)
half_tilt = sc_tilt / 2.0
c_tilt, s_tilt = float(np.cos(half_tilt)), float(np.sin(half_tilt))
half_h = HEADING / 2.0
c_head, s_head = float(np.cos(half_h)), float(np.sin(half_h))

# heading 회전 + tilt 쿼터니언 (간략화: 시저 방향만 표현)
SCISSOR_QUAT_A = np.array([0.9613, 0.0, 0.2756, 0.0])   # 올라간 상태 근사
SCISSOR_QUAT_B = np.array([0.9613, 0.0, -0.2756, 0.0])

VisualCuboid(
    prim_path="/World/AGV_scissor_a", name="agv_scissor_a",
    position=np.array([AGV_X - 0.06 * sin_h, AGV_Y + 0.06 * cos_h, sc_z]),
    orientation=SCISSOR_QUAT_A,
    scale=np.array([0.025, 0.04, 0.28]), color=C_SCISSOR,
)
VisualCuboid(
    prim_path="/World/AGV_scissor_b", name="agv_scissor_b",
    position=np.array([AGV_X + 0.06 * sin_h, AGV_Y - 0.06 * cos_h, sc_z]),
    orientation=SCISSOR_QUAT_B,
    scale=np.array([0.025, 0.04, 0.28]), color=C_SCISSOR,
)
VisualCuboid(
    prim_path="/World/AGV_lift_plate", name="agv_lift_plate",
    position=np.array([AGV_X, AGV_Y, LIFT_PLATE_UP]),
    scale=np.array([0.36, 0.36, 0.02]), color=C_SCISSOR,
)

# ─── 선반 빌드 (AGV 위, 올라간 상태) ─────────────────────────────────────────
dz = max(0.0, LIFT_PLATE_UP - LIFT_CONTACT_Z)   # 선반 Z 오프셋
SX, SY, SZ = AGV_X, AGV_Y, dz

shelf_root = "/World/Shelf"
UsdGeom.Xform.Define(stage, shelf_root)

# 루트 위치 설정
shelf_prim = stage.GetPrimAtPath(shelf_root)
xform = UsdGeom.Xformable(shelf_prim)
xform.AddTranslateOp().Set(Gf.Vec3d(SX, SY, SZ))

# 다리 4개
for i, (dx, dy) in enumerate(LEG_CORNERS):
    VisualCuboid(
        prim_path=f"{shelf_root}/col_{i}", name=f"shelf_col_{i}",
        position=np.array([dx * LEG_OFFSET, dy * LEG_OFFSET, LEG_HEIGHT / 2]),
        scale=np.array([LEG_SIZE, LEG_SIZE, LEG_HEIGHT]), color=C_FRAME,
    )
# 판 + 빔
for i, bz in enumerate(BOARD_ZS):
    beam_z   = bz - 0.032
    beam_len = BOARD_WIDTH - LEG_SIZE * 2
    beam_thk = 0.03
    VisualCuboid(
        prim_path=f"{shelf_root}/board_{i}", name=f"shelf_board_{i}",
        position=np.array([0.0, 0.0, bz]),
        scale=np.array([BOARD_WIDTH, BOARD_WIDTH, BOARD_THK]), color=C_SHELF_BOARD,
    )
    for bname, px, py, sx, sy in [
        ("beam_front",  0.0,        -LEG_OFFSET, beam_len, beam_thk),
        ("beam_back",   0.0,         LEG_OFFSET, beam_len, beam_thk),
        ("beam_left",  -LEG_OFFSET,  0.0,        beam_thk, beam_len),
        ("beam_right",  LEG_OFFSET,  0.0,        beam_thk, beam_len),
    ]:
        VisualCuboid(
            prim_path=f"{shelf_root}/{bname}_{i}", name=f"shelf_{bname}_{i}",
            position=np.array([px, py, beam_z]),
            scale=np.array([sx, sy, beam_thk]), color=C_FRAME,
        )
    # 트레이 + 소품
    TRAY_W, TRAY_D = 0.62, 0.62
    TRAY_BH, TRAY_WH, TRAY_WT = 0.02, 0.065, 0.025
    base_z = bz + BOARD_THK / 2
    VisualCuboid(
        prim_path=f"{shelf_root}/tray_bot_{i}", name=f"shelf_tray_bot_{i}",
        position=np.array([0.0, 0.0, base_z + TRAY_BH / 2]),
        scale=np.array([TRAY_W, TRAY_D, TRAY_BH]), color=C_TRAY,
    )
    inner_d = TRAY_D - TRAY_WT * 2
    for wname, px, py, sx, sy in [
        ("wf", 0.0,                        -(TRAY_D/2 - TRAY_WT/2), TRAY_W,  TRAY_WT),
        ("wb", 0.0,                         (TRAY_D/2 - TRAY_WT/2), TRAY_W,  TRAY_WT),
        ("wl", -(TRAY_W/2 - TRAY_WT/2),    0.0,                     TRAY_WT, inner_d),
        ("wr",  (TRAY_W/2 - TRAY_WT/2),    0.0,                     TRAY_WT, inner_d),
    ]:
        VisualCuboid(
            prim_path=f"{shelf_root}/tray_{wname}_{i}", name=f"shelf_tray_{wname}_{i}",
            position=np.array([px, py, base_z + TRAY_BH + TRAY_WH / 2]),
            scale=np.array([sx, sy, TRAY_WH]), color=C_TRAY,
        )
    rng = np.random.RandomState(11 * 31 + i * 7)
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
            prim_path=f"{shelf_root}/item_{i}_{ii}", name=f"shelf_item_{i}_{ii}",
            position=np.array([ix, iy, base_z + TRAY_BH + ih / 2]),
            scale=np.array([iw, id_, ih]), color=color,
        )

# ─── 작업대 + 작업자 배치 (AGV 오른쪽 +Y) ────────────────────────────────────
# WS 노드: (0, 1.2) → 컨베이어는 (-1.1, 1.2) 중심으로 -X 방향 연장
build_workstation(stage, ws_id=1, x=0.0, y=1.2)
build_worker(stage, ws_id=1, x=0.0, y=1.2)

# ─── 카메라 설정 ──────────────────────────────────────────────────────────────
# AGV + 작업대 동시에 담는 뷰
set_camera_view(
    eye=np.array([2.8, -1.0, 1.5]),
    target=np.array([-0.3, 0.5, 0.5]),
    camera_prim_path="/OmniverseKit_Persp",
)

world.reset()

# ─── 키보드 ──────────────────────────────────────────────────────────────────
_input_iface = carb.input.acquire_input_interface()
_keyboard = omni.appwindow.get_default_app_window().get_keyboard()
_s_was_down = False
_p_was_down = False
_heading_step = 0   # P 키로 AGV 회전 (45° 씩)

SAVE_DIR = os.path.dirname(os.path.abspath(__file__))

def _take_screenshot(idx: int):
    path = os.path.join(SAVE_DIR, f"agv_capture_{idx:02d}.png")
    try:
        import omni.kit.viewport.utility as vp_util
        vp = vp_util.get_active_viewport()
        vp_util.capture_viewport_to_file(vp, path)
        print(f"[Capture] 저장: {path}")
    except Exception as e:
        print(f"[Capture] 스크린샷 실패: {e}")
        print(f"  → Isaac Sim 창에서 직접 Ctrl+P 로 저장하세요.")

_shot_count = 0

print()
print("=" * 50)
print("  AGV 선반 운반 포즈 촬영 모드")
print("  S: 스크린샷 저장")
print("  P: AGV 45° 회전")
print("  창 닫으면 종료")
print("=" * 50)

for _ in range(20):
    simulation_app.update()

# ─── 메인 루프 ───────────────────────────────────────────────────────────────
while simulation_app.is_running():
    world.step(render=True)

    s_down = _input_iface.get_keyboard_value(_keyboard, carb.input.KeyboardInput.S) > 0.5
    p_down = _input_iface.get_keyboard_value(_keyboard, carb.input.KeyboardInput.P) > 0.5

    # S: 스크린샷
    if s_down and not _s_was_down:
        _take_screenshot(_shot_count)
        _shot_count += 1

    # P: AGV 45° 회전 후 씬 재구성
    if p_down and not _p_was_down:
        _heading_step = (_heading_step + 1) % 8
        new_h = _heading_step * np.pi / 4
        cos_h = float(np.cos(new_h))
        sin_h = float(np.sin(new_h))

        def _set_pos(path, x, y, z):
            prim = stage.GetPrimAtPath(path)
            if prim.IsValid():
                prim.GetAttribute("xformOp:translate").Set(Gf.Vec3d(x, y, z))

        _set_pos("/World/AGV_body", AGV_X, AGV_Y, BODY_Z)
        # 바퀴 재배치
        for i, (wdx, wdy) in enumerate(WHEEL_OFFSETS):
            rx = wdx * cos_h - wdy * sin_h
            ry = wdx * sin_h + wdy * cos_h
            _set_pos(f"/World/AGV_wheel_{i}", AGV_X + rx, AGV_Y + ry, WHEEL_Z)
        # 시저
        _set_pos("/World/AGV_scissor_a",
                 AGV_X - 0.06 * sin_h, AGV_Y + 0.06 * cos_h, sc_z)
        _set_pos("/World/AGV_scissor_b",
                 AGV_X + 0.06 * sin_h, AGV_Y - 0.06 * cos_h, sc_z)
        # 동체 방향
        half_h = new_h / 2.0
        q = Gf.Quatd(float(np.cos(half_h)), Gf.Vec3d(0.0, 0.0, float(np.sin(half_h))))
        body_prim = stage.GetPrimAtPath("/World/AGV_body")
        if body_prim.IsValid():
            attr = body_prim.GetAttribute("xformOp:orient")
            if attr and attr.IsValid():
                attr.Set(q)
        print(f"[Rotate] heading = {np.degrees(new_h):.0f}°")

    _s_was_down = s_down
    _p_was_down = p_down

simulation_app.close()
