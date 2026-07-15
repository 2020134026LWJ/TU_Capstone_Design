"""
실물 카메라 — 주원이 opencv_aruco_marker_detection.py 비전 로직 그대로 (UART/콘솔입력만 제거)
TU Capstone Design - AGV 물류 피킹 시스템

주원이 원본(opencv_aruco_marker_detection.py)의 '비전 부분'을 그대로 옮기고,
UART 송수신(serial)·콘솔 input·command/event 처리만 제거 → bridge_rpi가 담당.
주석도 주원이 원본 그대로 유지 (알아보기 쉽게).

bridge_rpi 연동:
  detect() → (marker_id, x_mm, y_mm, yaw_deg)
  rpi_main이 bridge.set_marker_offset(x, y, yaw) + bridge.publish_marker(id) 호출

※ Picamera2 + OpenCV — 라파에서만 동작 (그래서 cv2/picamera2는 메서드 안에서 import).
[TODO 미팅] yaw_deg(0~360, 마커 회전각) → 서버 heading(0=N/90=E) 변환 규약
"""

import pickle

from hardware.config import HEADING_OFFSET


def yaw_to_heading(yaw_deg: float) -> int:
    """카메라 yaw → 서버 heading (0=N / 90=E / 180=S / 270=W).

    실측(2026-07-12): **카메라가 시계방향으로 돌면 yaw가 증가한다. 1:1.**
    서버 heading도 나침반(시계방향 +)이라 **부호가 같다** → 덧셈만 하면 된다.

    4방향으로 반올림하는 이유: 우리는 90° 단위만 구분하면 되고, 그러면 **±45° 여유**가 생겨
    yaw 노이즈(실측 프레임간 중앙값 0.2°, 초점 나빠도 수 도)가 완전히 묻힌다.

    [주의] HEADING_OFFSET 은 실물에서 1회 실측해야 하는 값이다 (config 주석 참고).
    확정 전엔 서버가 이 값을 제어에 쓰지 않는다 — 비교/로그만 한다.
    """
    h = (float(yaw_deg) + HEADING_OFFSET) % 360.0
    return int(round(h / 90.0)) % 4 * 90
import numpy as np


class RpiCamera:
    """
    실시간으로 비디오를 받아 ArUco 마커를 검출하고 3D 포즈를 추정하는 클래스

    Args:
        calibration_file: 카메라 캘리브레이션 데이터(.pkl) 경로
            - camera_matrix: 카메라 내부 파라미터 행렬
            - dist_coeffs: 왜곡 계수
    """

    def __init__(self, calibration_file="camera_calibration.pkl", show_preview=True,
                 keep_frame=False):
        """keep_frame=True 면 detect()가 그린 프레임을 self.last_frame 에 남긴다.

        camera_preview(웹 프리뷰)가 **이 검출 결과를 그대로** 보여주기 위한 것.
        프리뷰가 자체적으로 다시 계산하면 화면의 숫자와 서버로 가는 숫자가 갈릴 수 있다.
        운용(rpi_main)에서는 기본 False라 비용 없음.
        """
        import cv2
        from picamera2 import Picamera2
        import time

        self.keep_frame = keep_frame
        self.last_frame = None

        # 캘리브레이션 데이터 추출
        with open(calibration_file, "rb") as f:
            calibration_data = pickle.load(f)
        self.camera_matrix = calibration_data['camera_matrix']
        self.dist_coeffs = calibration_data['dist_coeffs']
        self._cv2 = cv2

        # ArUco 검출기 설정
        aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
        aruco_params = cv2.aruco.DetectorParameters()
        self.detector = cv2.aruco.ArucoDetector(aruco_dict, aruco_params)

        # 마커 크기 및 3D 좌표 설정 (mm 단위)
        self.marker_size = 15
        # 마커 중심을 원점으로 하고, 네 꼭짓점의 위치를 정의
        half = self.marker_size / 2
        self.marker_3d_edges = np.array([
            [-half, half, 0],               # topLeft
            [half, half, 0],                # topRight
            [half, -half, 0],               # bottomRight
            [-half, -half, 0]               # bottomLeft
        ], dtype='float32')

        # 카메라 초기화
        self.picam2 = Picamera2()
        camera_config = self.picam2.create_preview_configuration()
        self.picam2.configure(camera_config)
        self.picam2.start()

        # (주원이 코드의 'usart pin8, 10 설정' → UART는 bridge_rpi가 담당하므로 여기선 제거)

        # 카메라 초기화 대기
        time.sleep(2)

        # ── (선택) undistort 최적화 — 주원이 신규 코드 그대로 ─────────────────
        # 매 프레임 cv2.undistort()를 호출하면 왜곡 맵을 매번 다시 계산해서 느리다.
        # 맵을 한 번만 만들어 두고 remap을 쓰면 루프 속도가 올라가 PID 피드백 주기가 빨라진다.
        frame0 = self.picam2.capture_array()
        h, w = frame0.shape[:2]
        self.map1, self.map2 = cv2.initUndistortRectifyMap(
            self.camera_matrix, self.dist_coeffs, None, self.camera_matrix, (w, h), cv2.CV_16SC2
        )

        # 미리보기 창 (헤드리스면 show_preview=False)
        self.show_preview = show_preview
        if show_preview:
            cv2.namedWindow('ArUco Marker Detection', cv2.WND_PROP_FULLSCREEN)
            cv2.setWindowProperty('ArUco Marker Detection', cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    def detect(self):
        """한 프레임 검출 → (marker_id, x_mm, y_mm, yaw_deg). 없으면 (None, None, None, None).

        주원이 live_aruco_detection while 루프 본문의 '비전 부분'을 한 프레임 단위로.
        (UART 수신/송신·콘솔 input은 bridge_rpi로 이동. 마커 여러 개면 첫 번째 반환)
        """
        cv2 = self._cv2

        # 프레임 읽기
        frame = self.picam2.capture_array()

        # BGR를 RGB로 변환
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 이미지 왜곡 보정 (미리 만든 맵으로 remap → undistort보다 빠름, 주원이 신규 코드 그대로)
        frame_undistorted = cv2.remap(frame_rgb, self.map1, self.map2, cv2.INTER_LINEAR)

        # 마커 검출
        corners, ids, rejected = self.detector.detectMarkers(frame_undistorted)

        result = (None, None, None, None)

        # 마커 ID 배열 평탄화 — OpenCV 버전별 모양 차이 흡수.
        #   4.x: ids = [[9], [17]]  (N,1) → ids[i][0]
        #   5.x: ids = [9, 17]      (N,)  → ids[i] 가 이미 스칼라 (ids[i][0]은 IndexError)
        # 두 버전 모두에서 동작하도록 여기서 1차원으로 펴서 marker_ids[i]로 쓴다.
        marker_ids = np.ravel(ids) if ids is not None else np.array([])

        # 마커가 검출되면 표시 및 포즈 추정
        if corners:
            for i, corner in enumerate(corners):
                # 코너 포인트 추출 및 표시
                corner = np.array(corner).reshape((4, 2))
                (topLeft, topRight, bottomRight, bottomLeft) = corner

                # 코너 포인트 좌표 변환
                topRightPoint = (int(topRight[0]), int(topRight[1]))
                topLeftPoint = (int(topLeft[0]), int(topLeft[1]))
                bottomRightPoint = (int(bottomRight[0]), int(bottomRight[1]))
                bottomLeftPoint = (int(bottomLeft[0]), int(bottomLeft[1]))

                # 코너 포인트 표시
                if self.show_preview:
                    cv2.circle(frame_undistorted, topLeftPoint, 4, (255, 0, 0), -1)
                    cv2.circle(frame_undistorted, topRightPoint, 4, (255, 0, 0), -1)
                    cv2.circle(frame_undistorted, bottomRightPoint, 4, (255, 0, 0), -1)
                    cv2.circle(frame_undistorted, bottomLeftPoint, 4, (255, 0, 0), -1)

                # PnP로 포즈 추정
                ret, rvec, tvec = cv2.solvePnP(
                    self.marker_3d_edges,
                    corner,
                    self.camera_matrix,
                    self.dist_coeffs
                )

                # 바닥에 있는 아루코 마커를 카메라가 위에서 촬영하는 구조
                # 카메라 중심을 기준으로 마커 중심의 x, y 거리를 구하고 yaw 각도를 구한다.

                # 거리 계산
                x = -round(tvec[0][0], 1)
                y = round(tvec[1][0], 1)

                # 각도 계산 (마커 회전)
                # cv2.Rodrigues: 벡터를 행렬로 변환하는 함수
                rotation_matrix, _ = cv2.Rodrigues(rvec)
                yaw = np.arctan2(rotation_matrix[1, 0], rotation_matrix[0, 0])
                yaw_deg = -round(np.rad2deg(yaw), 1)

                if (yaw_deg < 0.0):
                    yaw_deg = yaw_deg + 360.0
                elif (yaw_deg >= 360.0):
                    yaw_deg = yaw_deg - 360.0

                # 위치 및 회전 정보 표시
                if self.show_preview:
                    corner = corners[i][0]
                    pos = (int(topLeft[0]), int(topLeft[1]) - 10)
                    text = f"ID:{marker_ids[i]} ({x}, {y})mm {yaw_deg}deg"
                    cv2.putText(frame_undistorted, text, pos,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

                    # 좌표축 표시
                    cv2.drawFrameAxes(
                        frame_undistorted,      # 이미지
                        self.camera_matrix,     # 카메라 내부 파라미터 행렬
                        self.dist_coeffs,       # 카메라 왜곡 계수
                        rvec,                   # 회전 벡터
                        tvec,                   # 평행이동 벡터
                        self.marker_size / 2    # 축의 길이 (mm 단위)
                    )

                # 각도 전송
                # Write a string to UART Serial Port
                #   → 주원이 원본은 여기서 msg = f"<{command},..>" 를 uart.write() 했음.
                #     UART 송신은 bridge_rpi 담당 → 여기선 첫 마커 (id, x, y, yaw)만 반환.
                if result[0] is None:
                    result = (int(marker_ids[i]), float(x), float(y), float(yaw_deg))

        # 웹 프리뷰용 — 이 검출 결과가 그려진 프레임을 그대로 넘긴다
        if self.keep_frame:
            self.last_frame = frame_undistorted

        # 프레임 표시
        if self.show_preview:
            cv2.imshow('ArUco Marker Detection', frame_undistorted)
            # 'q' 키 처리는 rpi_main 루프에서 (여기선 창 갱신만)
            cv2.waitKey(1)

        return result

    def release(self):
        # 리소스 해제
        self.picam2.stop()
        if self.show_preview:
            self._cv2.destroyAllWindows()
        print("카메라 종료됨")
