"""
warehouse_4x8.wbt 월드 파일 생성 스크립트
- 4×8 그리드 (노드 1-32, 작업대 W1=33, W2=34)
- 선반 노드: 11,12,14,15,19,20,22,23
- 바닥 ArUco 마커 + KIVA 선반 + Pioneer3dx AGV
"""

import os

GRID_COLS = 8
GRID_ROWS = 4
CELL_SIZE = 1.0
MARKER_SIZE = 0.25  # 카메라 인식용 작은 마커

SHELF_NODES = {11, 12, 14, 15, 19, 20, 22, 23}

# 선반 정보: node_id -> (label, x, y)
SHELF_INFO = {
    11: ("1-1", 2.5, 1.5),
    12: ("1-2", 3.5, 1.5),
    14: ("1-3", 5.5, 1.5),
    15: ("1-4", 6.5, 1.5),
    19: ("2-1", 2.5, 2.5),
    20: ("2-2", 3.5, 2.5),
    22: ("2-3", 5.5, 2.5),
    23: ("2-4", 6.5, 2.5),
}


def node_to_world(node_id):
    if node_id == 33:  # W1
        return -0.5, 0.5
    elif node_id == 34:  # W2
        return -0.5, 3.5
    idx = node_id - 1
    col = idx % GRID_COLS
    row = idx // GRID_COLS
    return (col + 0.5) * CELL_SIZE, (row + 0.5) * CELL_SIZE


def gen_aruco_marker_solid(node_id, x, y, size=MARKER_SIZE):
    """ArUco 텍스처 마커 Solid 생성"""
    return f"""Solid {{
  translation {x} {y} 0.001
  children [
    Shape {{
      appearance Appearance {{
        material Material {{
          diffuseColor 1 1 1
        }}
        texture ImageTexture {{
          url ["textures/aruco_markers/aruco_{node_id}.png"]
        }}
      }}
      geometry Plane {{
        size {size} {size}
      }}
    }}
  ]
  name "aruco_{node_id}"
}}
"""


def gen_shelf(node_id, label, x, y):
    """KIVA 3D 선반 생성"""
    return f"""DEF SHELF_{node_id} Solid {{
  translation {x} {y} 0
  children [
    Transform {{ translation -0.35 -0.35 0.14 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.6 0.6 0.6 metalness 0.3 }} geometry Cylinder {{ radius 0.03 height 0.28 }} }} ] }}
    Transform {{ translation 0.35 -0.35 0.14 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.6 0.6 0.6 metalness 0.3 }} geometry Cylinder {{ radius 0.03 height 0.28 }} }} ] }}
    Transform {{ translation -0.35 0.35 0.14 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.6 0.6 0.6 metalness 0.3 }} geometry Cylinder {{ radius 0.03 height 0.28 }} }} ] }}
    Transform {{ translation 0.35 0.35 0.14 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.6 0.6 0.6 metalness 0.3 }} geometry Cylinder {{ radius 0.03 height 0.28 }} }} ] }}
    Transform {{ translation 0 0 0.28 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.2 0.4 0.9 roughness 0.5 }} geometry Box {{ size 0.78 0.78 0.02 }} }} ] }}
    Transform {{ translation 0 0 0.43 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.2 0.8 0.2 roughness 0.5 }} geometry Box {{ size 0.78 0.78 0.02 }} }} ] }}
    Transform {{ translation 0 0 0.58 children [ Shape {{ appearance PBRAppearance {{ baseColor 0.9 0.2 0.2 roughness 0.5 }} geometry Box {{ size 0.78 0.78 0.02 }} }} ] }}
  ]
  name "shelf_{node_id}"
}}
"""


def gen_robot(rid, x, y, start_node):
    """Pioneer3dx 로봇 생성 - 하향 카메라 + GPS/Compass + 리프트"""
    return f"""DEF AGV_{rid} Pioneer3dx {{
  translation {x} {y} 0
  rotation 0 0 1 0
  name "AGV_{rid}"
  supervisor TRUE
  controller "agv_mqtt_controller"
  controllerArgs ["{rid}", "{start_node}"]
  extensionSlot [
    GPS {{
      name "gps"
    }}
    Compass {{
      name "compass"
    }}
    Camera {{
      translation 0 0 0.02
      rotation 1 0 0 3.14159
      name "floor_camera"
      fieldOfView 1.2
      width 320
      height 240
      near 0.01
    }}
    SliderJoint {{
      jointParameters JointParameters {{
        axis 0 0 1
      }}
      device [
        LinearMotor {{
          name "lift_motor"
          maxForce 100
          minPosition 0
          maxPosition 0.10
        }}
        PositionSensor {{
          name "lift_sensor"
        }}
      ]
      endPoint Solid {{
        translation 0 0 0.0
        children [
          Shape {{
            appearance PBRAppearance {{
              baseColor 1.0 0.6 0.0
              roughness 0.3
              metalness 0.5
            }}
            geometry Cylinder {{
              radius 0.15
              height 0.02
            }}
          }}
        ]
        name "lift_platform"
        boundingObject Cylinder {{
          radius 0.15
          height 0.02
        }}
        physics Physics {{
          density -1
          mass 0.5
        }}
      }}
    }}
  ]
}}
"""


def main():
    lines = []

    # Header
    lines.append('#VRML_SIM R2023b utf8\n')
    lines.append('EXTERNPROTO "https://raw.githubusercontent.com/cyberbotics/webots/R2023b/projects/objects/backgrounds/protos/TexturedBackground.proto"')
    lines.append('EXTERNPROTO "https://raw.githubusercontent.com/cyberbotics/webots/R2023b/projects/objects/backgrounds/protos/TexturedBackgroundLight.proto"')
    lines.append('EXTERNPROTO "https://raw.githubusercontent.com/cyberbotics/webots/R2023b/projects/objects/floors/protos/RectangleArena.proto"')
    lines.append('EXTERNPROTO "https://raw.githubusercontent.com/cyberbotics/webots/R2023b/projects/robots/adept/pioneer3/protos/Pioneer3dx.proto"\n')

    # WorldInfo
    lines.append("""WorldInfo {
  info [
    "AGV Warehouse Simulation - 4x8 Grid with ArUco Markers"
    "TU Capstone Design Project"
  ]
  title "AGV Warehouse 4x8 ArUco"
  basicTimeStep 16
  contactProperties [
    ContactProperties {
      material1 "floor"
      material2 "wheel"
      coulombFriction [2]
    }
  ]
}
""")

    # Viewpoint - 탑뷰 (4x8 그리드 중심)
    lines.append("""Viewpoint {
  orientation -1 0 0 1.5708
  position 3.5 2.0 12
}
""")

    # Background & Light
    lines.append('TexturedBackground {\n  texture "factory"\n}\n')
    lines.append('TexturedBackgroundLight {\n}\n')

    # Floor (4×8 그리드 + 작업대 영역)
    lines.append("""RectangleArena {
  translation 3.5 2.0 0
  floorSize 10 6
  floorTileSize 1 1
  floorAppearance PBRAppearance {
    baseColor 0.9 0.9 0.9
    roughness 1
    metalness 0
  }
  wallHeight 0.1
  wallAppearance PBRAppearance {
    baseColor 0.3 0.3 0.3
    roughness 0.5
  }
}
""")

    # ArUco markers for grid nodes 1-32
    lines.append("# ============================================================")
    lines.append("# ArUco Floor Markers (nodes 1-32)")
    lines.append("# ============================================================\n")

    for node_id in range(1, 33):
        x, y = node_to_world(node_id)
        lines.append(gen_aruco_marker_solid(node_id, x, y))

    # Workstation markers (33=W1, 34=W2)
    lines.append("# ============================================================")
    lines.append("# Workstation ArUco Markers (33=W1, 34=W2)")
    lines.append("# ============================================================\n")

    for node_id in [33, 34]:
        x, y = node_to_world(node_id)
        lines.append(gen_aruco_marker_solid(node_id, x, y, size=0.35))

    # Workstation visual indicators
    lines.append("# ============================================================")
    lines.append("# Workstation Indicators")
    lines.append("# ============================================================\n")

    for ws_id, ws_name, wx, wy in [(33, "W1", -0.5, 0.5), (34, "W2", -0.5, 3.5)]:
        lines.append(f"""Solid {{
  translation {wx} {wy} 0.002
  children [
    Shape {{
      appearance PBRAppearance {{
        baseColor 1 0.8 0
        roughness 0.3
        metalness 0
      }}
      geometry Plane {{
        size 0.8 0.8
      }}
    }}
  ]
  name "ws_{ws_name}"
}}
""")

    # Shelves
    lines.append("# ============================================================")
    lines.append("# KIVA-Style 3-Tier Shelves (8 units)")
    lines.append("# ============================================================\n")

    for node_id, (label, x, y) in SHELF_INFO.items():
        lines.append(gen_shelf(node_id, label, x, y))

    # Robots
    lines.append("# ============================================================")
    lines.append("# AGV Robots (Pioneer 3-DX) with Camera + Supervisor + Lift")
    lines.append("# ============================================================\n")

    lines.append("# Robot 1 - starts at W1 (node 33)")
    lines.append(gen_robot(1, -0.5, 0.5, 33))
    lines.append("# Robot 2 - starts at W2 (node 34)")
    lines.append(gen_robot(2, -0.5, 3.5, 34))

    # Write
    out_path = os.path.join(os.path.dirname(__file__), "worlds", "warehouse_4x8.wbt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Generated: {out_path}")


if __name__ == "__main__":
    main()
