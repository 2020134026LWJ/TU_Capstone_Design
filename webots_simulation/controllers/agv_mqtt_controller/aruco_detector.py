"""공통 ArUco 마커 감지 모듈 (실물/시뮬 동일)"""

from typing import Optional

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False


class ArucoDetector:
    """ArUco 4x4 마커 감지기"""

    def __init__(self, rid: int):
        self._rid = rid
        self._detector = None
        self._last_marker = None

        if _CV2_AVAILABLE:
            try:
                aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_100)
                aruco_params = cv2.aruco.DetectorParameters()
                self._detector = cv2.aruco.ArucoDetector(aruco_dict, aruco_params)
            except AttributeError:
                pass

    @property
    def available(self) -> bool:
        return self._detector is not None

    @property
    def detected_marker(self) -> Optional[int]:
        return self._last_marker

    def detect(self, gray) -> Optional[int]:
        """그레이스케일 이미지에서 ArUco 마커 감지 → 마커 ID 반환"""
        if self._detector is None or gray is None:
            self._last_marker = None
            return None

        corners, ids, _ = self._detector.detectMarkers(gray)

        if ids is not None and len(ids) > 0:
            marker_id = int(ids[0][0])
            if self._last_marker != marker_id:
                print(f"[AGV {self._rid}] Marker detected: {marker_id}")
            self._last_marker = marker_id
            return marker_id

        self._last_marker = None
        return None
