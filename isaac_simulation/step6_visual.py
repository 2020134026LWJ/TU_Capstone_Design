"""
Step 6 — 바퀴 회전 + 차체 방향 전환 + 시각적 현실감 개선

step5_camera_aruco.py 기반 변경사항:
  1. 모터 추상화 (hardware/isaac_hw.py):
       IsaacMotors.set_speeds(left, right) — RPi/STM32 와 동일 시그니처
       실물 전환 시 IsaacMotors → RaspiMotors 교체만 하면 됨
  2. Navigator 방식 이동 (Webots 와 동일):
       TURNING (제자리 회전) → MOVING (직진+각도 보정)
  3. 바퀴 회전: 실제 선속도 기반 wheel_angle 누적
  4. 시각 개선: 색상 팔레트 · 센서 범프 · LED · 작업대 두께
  5. CAD 경로 설정 (CAD_PATHS):
       None = 기본 도형 사용
       경로 지정 시 USD 파일 로드 (CAD 파일 교체 포인트)

실행:
    ~/isaacsim/_build/linux-x86_64/release/python.sh \\
        /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step6_visual.py
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
from isaacsim.core.api.objects import VisualCuboid, VisualCylinder, VisualSphere
from isaacsim.core.utils.viewports import set_camera_view

import paho.mqtt.client as mqtt_lib

from hardware.isaac_hw import IsaacMotors

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

# ─── 설정 파일 경로 ───────────────────────────────────────────────────────────
_HERE      = os.path.dirname(os.path.abspath(__file__))
_ROOT      = os.path.join(_HERE, "..")
MAP_PATH   = os.path.join(_ROOT, "server", "map.json")
SHELF_PATH = os.path.join(_ROOT, "server", "shelf_config.json")
ROBOT_PATH = os.path.join(_ROOT, "server", "robot_config.json")
ARUCO_DIR  = os.path.join(_ROOT, "webots_simulation", "textures", "aruco_markers")

with open(MAP_PATH)   as f: map_cfg   = json.load(f)
with open(SHELF_PATH) as f: shelf_cfg = json.load(f)
with open(ROBOT_PATH) as f: robot_cfg = json.load(f)

nodes          = {n["id"]: n for n in map_cfg["nodes"]}
shelf_node_ids = set(map_cfg["shelf_nodes"])
ws_node_ids    = set(map_cfg["workstation_nodes"])
robot_homes    = {int(rid): info["home_node"]
                  for rid, info in robot_cfg["robots"].items()}


def node_xy(node_id: int) -> np.ndarray:
    n = nodes[node_id]
    return np.array([n["x"], n["y"]], dtype=float)


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
        self.current_node = home_node

        # ── 모터 드라이버 (교체 포인트) ──────────────────────────────────────
        # 실물 전환 시: IsaacMotors() → RaspiMotors()
        #   from hardware.raspi_hw import RaspiMotors
        #   self.motors = RaspiMotors()
        self.motors = IsaacMotors()

        # ── 이동 상태 ──────────────────────────────────────────────────────
        # IDLE / TURNING / MOVING / NODE_WAIT
        self.state            = "IDLE"
        self.path_queue       = []
        self.goal_node        = None
        self.heading          = 0.0   # 현재 방향 (모터 구동으로 변화)
        self.heading_target   = 0.0   # TURNING 목표 방향

        home = node_xy(home_node)
        self.pos             = home.copy()
        self.target_pos      = home.copy()
        self._moving_to_node = home_node

        # ── 리프트 ────────────────────────────────────────────────────────
        self.lift_z         = self.LIFT_PLATE_Z
        self.lift_target_z  = self.LIFT_PLATE_Z
        self.lift_state     = "IDLE"
        self.carrying_shelf = None

        # ── 시각 (Step 6) ─────────────────────────────────────────────────
        self.wheel_angle = 0.0  # 이동 거리 기반 누적 회전각

        # ── CAD 모드 여부 ─────────────────────────────────────────────────
        # CAD_PATHS["agv"] 지정 시 True → _sync_prim 이 루트 prim 만 이동
        self._use_cad = bool(CAD_PATHS.get("agv"))

        # ── 콜백 ──────────────────────────────────────────────────────────
        self.on_arrived      = None
        self.on_intermediate = None
        self.on_lift_done    = None

        self._last_marker: int | None = None

        # MQTT 스레드 → main loop 핸드오프
        self._pending_plan:   tuple | None = None
        self._pending_resume: bool         = False

    # ─── 이동 ────────────────────────────────────────────────────────────────

    def set_plan(self, path_queue: list, goal: int):
        self.path_queue   = list(path_queue)
        self.goal_node    = goal
        self._last_marker = None
        self._move_to_next()

    def _move_to_next(self):
        if not self.path_queue:
            return
        next_node            = self.path_queue.pop(0)
        self._moving_to_node = next_node
        self.target_pos      = node_xy(next_node)

        diff = self.target_pos - self.pos
        if np.linalg.norm(diff) < POSITION_TOLERANCE:
            self.state = "MOVING"
            return

        self.heading_target = float(np.arctan2(diff[1], diff[0]))
        angle_diff = _normalize_angle(self.heading_target - self.heading)
        if abs(angle_diff) < ANGLE_TOLERANCE:
            self.state = "MOVING"
        else:
            self.motors.stop()
            self.state = "TURNING"

    def resume(self):
        if self.state == "NODE_WAIT" and self.path_queue:
            self._last_marker = None
            self._move_to_next()

    # ─── 리프트 명령 ─────────────────────────────────────────────────────────

    def cmd_pickup(self, shelf_id: int):
        self.carrying_shelf = shelf_id
        self.lift_target_z  = self.LIFT_PLATE_UP
        self.lift_state     = "RAISING"
        print(f"[AGV {self.rid}] Lift RAISING -> shelf {shelf_id}")

    def cmd_putdown(self, shelf_id: int):
        self.lift_target_z = self.LIFT_PLATE_Z
        self.lift_state    = "LOWERING"
        print(f"[AGV {self.rid}] Lift LOWERING -> shelf {shelf_id}")

    # ─── 업데이트 ────────────────────────────────────────────────────────────

    def update(self, dt: float, stage):
        self._update_move(dt, stage)
        self._update_lift(dt, stage)

    def _update_move(self, dt: float, stage):
        if self.state == "TURNING":
            diff = _normalize_angle(self.heading_target - self.heading)
            if abs(diff) < ANGLE_TOLERANCE:
                self.motors.stop()
                self.state = "MOVING"
            else:
                # 목표에 가까울수록 속도 감소 (오버슈트 방지)
                speed = min(1.0, abs(diff) / 0.5) * TURN_SPEED
                if diff > 0:
                    self.motors.set_speeds(-speed,  speed)  # 반시계
                else:
                    self.motors.set_speeds( speed, -speed)  # 시계
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
                # 직진 + 작은 각도 보정 (Webots Navigator 와 동일 방식)
                angle_to_target = float(np.arctan2(diff[1], diff[0]))
                err = _normalize_angle(angle_to_target - self.heading)
                correction = max(-0.5, min(0.5, err * 2.0))
                self.motors.set_speeds(MOVE_SPEED - correction,
                                       MOVE_SPEED + correction)
                v, omega = self.motors.get_velocity()
                self.heading = _normalize_angle(self.heading + omega * dt)
                self.pos += v * dt * np.array([np.cos(self.heading),
                                               np.sin(self.heading)])
                # 실제 이동 거리 기반 바퀴 회전각 누적
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
                print(f"[AGV {self.rid}] Lift UP done (shelf {self.carrying_shelf})")
                if self.on_lift_done:
                    self.on_lift_done(self.rid, self.carrying_shelf, "pickup")
            elif prev_state == "LOWERING":
                shelf_id = self.carrying_shelf
                self.carrying_shelf = None
                self._place_shelf(stage, shelf_id)
                print(f"[AGV {self.rid}] Lift DOWN done (shelf {shelf_id})")
                if self.on_lift_done:
                    self.on_lift_done(self.rid, shelf_id, "putdown")
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

    def _sync_shelf(self, stage):
        """선반 위치/방향 동기화 — AGV 절대 좌표 + heading 회전 적용"""
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
                op.Set(Gf.Vec3d(x, y, dz))
                has_t = True
            elif op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                self._set_quat_op(op, cw, 0.0, 0.0, sw)
                has_o = True
        if not has_t:
            xform.AddTranslateOp().Set(Gf.Vec3d(x, y, dz))
        if not has_o:
            xform.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(
                Gf.Quatf(cw, Gf.Vec3f(0.0, 0.0, sw)))

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
            elif op.GetOpType() == UsdGeom.XformOp.TypeOrient:
                self._set_quat_op(op, 1.0, 0.0, 0.0, 0.0)

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
# MQTTBridge (step5 와 동일)
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

    def _handle_plan(self, data):
        for r in data.get("robots", []):
            rid = int(r.get("rid", -1))
            agv = self.agvs.get(rid)
            if agv is None:
                continue
            node_path = r.get("node_path", [])
            goal      = r.get("goal")
            if node_path and len(node_path) > 1:
                agv._pending_plan = (node_path, goal)
                print(f"[AGV {rid}] Plan queued: {node_path} -> goal {goal}")
            elif goal is not None:
                agv.current_node = goal
                self._publish_arrived(rid, goal)

    def _handle_control(self, data):
        rid = int(data.get("rid", -1))
        agv = self.agvs.get(rid)
        if agv and data.get("cmd") == "resume":
            agv._pending_resume = True
            print(f"[AGV {rid}] <- resume queued")

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

    def _on_arrived(self, rid: int, node: int):
        self._publish_arrived(rid, node)

    def _on_intermediate(self, rid: int, node: int):
        self._publish_position(rid, node)

    def _on_lift_done(self, rid: int, shelf_id: int, command: str):
        msg = {"rid": rid, "command": command, "shelf_id": shelf_id, "status": "done"}
        self._client.publish(TOPIC_SHELF_ACK, json.dumps(msg))
        print(f"[AGV {rid}] -> /agv/shelf_ack  {command} shelf {shelf_id}")

    def publish_marker_detected(self, rid: int, marker_id: int):
        msg = {"rid": rid, "marker_id": marker_id, "ts": int(time.time())}
        self._client.publish(TOPIC_MARKER, json.dumps(msg))
        print(f"[CAM {rid}] -> /agv/marker  marker_id={marker_id}")

    def _publish_arrived(self, rid: int, node: int):
        msg = {"type": "robot_arrived", "rid": rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))
        print(f"[AGV {rid}] -> /agv/arrived  node={node}")

    def _publish_position(self, rid: int, node: int):
        msg = {"type": "robot_position", "rid": rid, "node": node}
        self._client.publish(TOPIC_ARRIVED, json.dumps(msg))

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()


# ═══════════════════════════════════════════════════════════════════════════════
# 색상 팔레트
# ═══════════════════════════════════════════════════════════════════════════════

C_FRAME       = np.array([0.20, 0.20, 0.22])
C_SHELF_BOARD = np.array([0.82, 0.74, 0.60])
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
C_WALL        = np.array([0.88, 0.88, 0.85])  # 창고 벽 (밝은 회색)
C_TRAY        = np.array([0.65, 0.65, 0.70])  # 금속 트레이

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


def build_workstation(stage, node_id: int, x: float, y: float):
    """작업대 빌드 — X방향 컨베이어 벨트, 선반과 90도 각도 배치

    배치:
      (x,    y)       선반/AGV 도착 (WS 노드)
      (x-0.3, y+0.45) 작업자 (선반 집고 컨베이어에 올리는 위치)
      (x-0.7, y+1.0)  컨베이어 중심 (X방향으로 흐름)
      (x-1.7, y+1.0)  수납 박스 (-X 끝)
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
    CONV_CX  = x - 0.70   # 컨베이어 중심 X
    CONV_CY  = y + 1.00   # 컨베이어 중심 Y (선반 옆 90도 방향)

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

    # 기본 도형
    VisualCuboid(
        prim_path=f"/World/AGV_{rid}_body", name=f"agv_{rid}_body",
        position=np.array([x, y, IsaacAGV.BODY_Z]),
        scale=np.array([0.38, 0.38, 0.08]), color=c_agv,
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


def build_worker(stage, ws_node_id: int, x: float, y: float):
    """작업대 옆 작업자 프록시 — 실린더 몸통 + 구 머리 + 헬멧"""
    root = f"/World/Worker_{ws_node_id}"
    UsdGeom.Xform.Define(stage, root)

    # 선반(x, y)과 컨베이어(x-0.7, y+1.0) 사이에 서 있음
    # 조금만 이동해서 선반에서 물건을 꺼내 컨베이어에 올릴 수 있는 위치
    wx = x - 0.30
    wy = y + 0.45

    # 하체 (바지)
    VisualCylinder(
        prim_path=f"{root}/legs", name=f"worker_{ws_node_id}_legs",
        position=np.array([wx, wy, 0.30]),
        radius=0.10, height=0.55, color=np.array([0.20, 0.25, 0.55]),
    )
    # 상체 (작업복)
    VisualCylinder(
        prim_path=f"{root}/torso", name=f"worker_{ws_node_id}_torso",
        position=np.array([wx, wy, 0.72]),
        radius=0.12, height=0.44, color=C_WORKER_SUIT,
    )
    # 머리
    VisualSphere(
        prim_path=f"{root}/head", name=f"worker_{ws_node_id}_head",
        position=np.array([wx, wy, 1.07]),
        radius=0.12, color=C_WORKER_HEAD,
    )
    # 안전모
    VisualCylinder(
        prim_path=f"{root}/helmet", name=f"worker_{ws_node_id}_helmet",
        position=np.array([wx, wy, 1.17]),
        radius=0.15, height=0.07, color=np.array([1.0, 0.85, 0.0]),
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
world.scene.add_default_ground_plane()
stage = omni.usd.get_context().get_stage()

for node_id in shelf_node_ids:
    node = nodes[node_id]
    build_shelf(stage, node_id, node["x"], node["y"])

for node_id in ws_node_ids:
    node = nodes[node_id]
    build_workstation(stage, node_id, node["x"], node["y"])
    build_worker(stage, node_id, node["x"], node["y"])

build_warehouse_env(stage)

agvs: dict[int, IsaacAGV] = {}
for rid, home_node in sorted(robot_homes.items()):
    n = nodes[home_node]
    x, y = n["x"], n["y"]
    build_agv(stage, rid, x, y)
    agvs[rid] = IsaacAGV(rid, home_node)
    print(f"[AGV {rid}] Home: node {home_node}  ({x}, {y})")

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

print("  선반/작업대/AGV 배치 완료")

for node_id, node in nodes.items():
    create_aruco_marker(stage, node_id, node["x"], node["y"])
print(f"  ArUco 마커 배치 완료: {len(nodes)}개")

bridge = MQTTBridge(agvs)
bridge.connect()

set_camera_view(
    eye=np.array([3.5, -4.0, 10.0]),
    target=np.array([3.5, 2.0, 0.0]),
    camera_prim_path="/OmniverseKit_Persp",
)

print()
print("=" * 60)
print("  Isaac Sim 5.1.0 — Step 6: 모터 추상화 + 차동구동 + 시각")
print("=" * 60)
print(f"  AGV-1 홈: {robot_homes[1]}  AGV-2 홈: {robot_homes[2]}")
print(f"  이동 방식: TURNING({TURN_SPEED:.2f}m/s) -> MOVING({MOVE_SPEED}m/s)")
print(f"  모터: IsaacMotors  [실물 전환: IsaacMotors -> RaspiMotors]")
cad_active = [k for k, v in CAD_PATHS.items() if v]
print(f"  CAD: {'활성 ' + str(cad_active) if cad_active else '없음 (기본 도형)'}")
print("-" * 60)
print("  창을 닫으면 종료됩니다.")
print("=" * 60)
print()

for _ in range(10):
    simulation_app.update()

# ─── 메인 루프 ────────────────────────────────────────────────────────────────
frame_count = 0

while simulation_app.is_running():
    dt = world.get_physics_dt()
    world.step(render=True)

    for agv in agvs.values():
        if agv._pending_plan is not None:
            node_path, goal = agv._pending_plan
            agv._pending_plan = None
            start_node = node_path[0]
            if agv.state == "IDLE":
                # IDLE 로봇도 resume 메커니즘을 거치도록 NODE_WAIT으로 전환
                # (자동 이동 시 서버의 충돌 회피 로직 우회 방지)
                agv.pos          = node_xy(start_node).copy()
                agv.path_queue   = list(node_path[1:])
                agv.goal_node    = goal
                agv._last_marker = None
                agv.state        = "NODE_WAIT"
                bridge._publish_arrived(agv.rid, start_node)
                print(f"[AGV {agv.rid}] Plan received (IDLE→NODE_WAIT) at {start_node} → requesting resume")
            elif agv.state == "NODE_WAIT" and agv.current_node == start_node:
                agv.path_queue   = list(node_path[1:])
                agv.goal_node    = goal
                agv._last_marker = None
                bridge._publish_arrived(agv.rid, start_node)
                print(f"[AGV {agv.rid}] Re-arrived at {start_node} -> trigger server resume")
            else:
                agv.path_queue   = list(node_path[1:])
                agv.goal_node    = goal
                agv._last_marker = None

        if agv._pending_resume:
            agv._pending_resume = False
            agv.resume()

        agv.update(dt, stage)

    frame_count += 1
    if frame_count % DETECT_INTERVAL == 0:
        for rid, agv in agvs.items():
            if agv.state not in ("MOVING", "TURNING", "NODE_WAIT"):
                continue
            for node_id, node in nodes.items():
                dist = float(np.linalg.norm(
                    agv.pos - np.array([node["x"], node["y"]])
                ))
                if dist > CAM_DETECT_RADIUS:
                    continue
                if node_id == agv._last_marker:
                    continue
                agv._last_marker = node_id
                bridge.publish_marker_detected(rid, node_id)

bridge.disconnect()
simulation_app.close()
