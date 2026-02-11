"""
ArUco 마커 이미지 생성 스크립트
노드 1~51에 대응하는 DICT_4X4_100 마커 PNG 생성
마커 ID = 노드 ID (1~51)
"""

import cv2
import numpy as np
import os

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "textures", "aruco_markers")
DICT_TYPE = cv2.aruco.DICT_4X4_100
MARKER_PX = 400  # 마커 이미지 크기 (px)
BORDER_PX = 56   # 흰색 테두리 (px) → 총 512x512 (2의 거듭제곱)

os.makedirs(OUTPUT_DIR, exist_ok=True)

aruco_dict = cv2.aruco.getPredefinedDictionary(DICT_TYPE)

for node_id in range(1, 52):  # 1~51
    marker_img = cv2.aruco.drawMarker(aruco_dict, node_id, MARKER_PX)

    # 흰색 테두리 추가 (검출 안정성 향상)
    bordered = np.full(
        (MARKER_PX + BORDER_PX * 2, MARKER_PX + BORDER_PX * 2),
        255, dtype=np.uint8
    )
    bordered[BORDER_PX:BORDER_PX + MARKER_PX, BORDER_PX:BORDER_PX + MARKER_PX] = marker_img

    path = os.path.join(OUTPUT_DIR, f"aruco_{node_id}.png")
    cv2.imwrite(path, bordered)

print(f"Generated {51} ArUco markers (DICT_4X4_100, ID 1-51) in {OUTPUT_DIR}")
