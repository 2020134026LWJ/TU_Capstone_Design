"""
실물 카메라 — 주원이 opencv_arucomarker_detection_v4.py 비전 로직 기반
TU Capstone Design - AGV 물류 피킹 시스템

bridge_rpi 연동:
  detect() → (marker_id, x_mm, y_mm, yaw_deg)
  rpi_main이 이를 받아 bridge.set_marker_offset(x, y, yaw) + bridge.publish_marker(id, heading)

※ 주원이 opencv 코드의 "비전 부분"(ArUco + solvePnP offset 계산)만 복사.
  UART 송신·콘솔 입력은 bridge_rpi가 담당 (역할 분리).
※ Picamera2 + OpenCV — 라파에서만 동작 (PC에선 picamera2/cv2 import 불가).

[TODO 미팅] yaw_deg(0~360) → 서버 heading(0=N/90=E) 변환 규약 확인 (지금은 yaw_deg 그대로 발행)
"""

import pickle
import numpy as np


class RpiCamera:
    """라파 카메라 (Picamera2 + OpenCV ArUco). 바닥 마커 offset/yaw 추정."""

    def __init__(self, calibration_file="camera_calibration.pkl"):
        import cv2
        from picamera2 import Picamera2
        import time

        with open(calibration_file, "rb") as f:
            cal = pickle.load(f)
        self._cm = cal["camera_matrix"]
        self._dc = cal["dist_coeffs"]
        self._cv2 = cv2

        # 주원이 코드: DICT_4X4_250, 마커 한 변 25mm
        aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
        self._detector = cv2.aruco.ArucoDetector(aruco_dict, cv2.aruco.DetectorParameters())
        h = 25 / 2.0
        self._marker_3d = np.array([
            [-h,  h, 0], [ h,  h, 0], [ h, -h, 0], [-h, -h, 0]   # TL TR BR BL
        ], dtype="float32")

        self._picam2 = Picamera2()
        self._picam2.configure(self._picam2.create_preview_configuration())
        self._picam2.start()
        time.sleep(2)  # 카메라 워밍업

    def detect(self):
        """ArUco 감지 → (marker_id, x_mm, y_mm, yaw_deg). 없으면 (None, None, None, None)."""
        cv2 = self._cv2
        frame = self._picam2.capture_array()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        und = cv2.undistort(rgb, self._cm, self._dc)

        corners, ids, _ = self._detector.detectMarkers(und)
        if ids is None or len(ids) == 0:
            return None, None, None, None

        # 첫 마커 기준 포즈 추정 (주원이 코드와 동일)
        _, rvec, tvec = cv2.solvePnP(self._marker_3d, corners[0], self._cm, self._dc)
        x = -round(float(tvec[0][0]), 1)        # 주원이: x = -tvec[0]
        y = round(float(tvec[1][0]), 1)         # 주원이: y =  tvec[1]
        R, _ = cv2.Rodrigues(rvec)
        yaw = np.arctan2(R[1, 0], R[0, 0])
        yaw_deg = -round(float(np.rad2deg(yaw)), 1)
        if yaw_deg < 0.0:
            yaw_deg += 360.0
        elif yaw_deg >= 360.0:
            yaw_deg -= 360.0

        return int(ids[0][0]), x, y, yaw_deg

    def release(self):
        self._picam2.stop()
