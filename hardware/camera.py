"""
실물 카메라 — 주원이 opencv_arucomarker_detection_v4.py 비전 로직 그대로 (UART/콘솔입력만 제거)
TU Capstone Design - AGV 물류 피킹 시스템

주원이 원본(opencv_arucomarker_detection_v4.py)의 '비전 부분'을 그대로 옮기고,
UART 송수신(serial)·콘솔 input·command/event 처리만 제거 → bridge_rpi가 담당.
주석도 주원이 원본 그대로 유지 (알아보기 쉽게).

bridge_rpi 연동:
  detect() → (marker_id, x_mm, y_mm, yaw_deg)
  rpi_main이 bridge.set_marker_offset(x, y, yaw) + bridge.publish_marker(id) 호출

※ Picamera2 + OpenCV — 라파에서만 동작 (그래서 cv2/picamera2는 메서드 안에서 import).
[TODO 미팅] yaw_deg(0~360, 마커 회전각) → 서버 heading(0=N/90=E) 변환 규약
"""

import pickle
import numpy as np


class RpiCamera:
    """
    실시간으로 비디오를 받아 ArUco 마커를 검출하고 3D 포즈를 추정하는 클래스

    Args:
        calibration_file: 카메라 캘리브레이션 데이터(.pkl) 경로
            - camera_matrix: 카메라 내부 파라미터 행렬
            - dist_coeffs: 왜곡 계수
    """

    def __init__(self, calibration_file="camera_calibration.pkl", show_preview=True):
        import cv2
        from picamera2 import Picamera2
        import time

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
        self.marker_size = 25
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

        # 이미지 왜곡 보정 (캘리브레이션 적용)
        frame_undistorted = cv2.undistort(frame_rgb, self.camera_matrix, self.dist_coeffs)

        # 마커 검출
        corners, ids, rejected = self.detector.detectMarkers(frame_undistorted)

        result = (None, None, None, None)

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
                    text = f"ID:{ids[i][0]} ({x}, {y})mm {yaw_deg}deg"
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
                    result = (int(ids[i][0]), float(x), float(y), float(yaw_deg))

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
