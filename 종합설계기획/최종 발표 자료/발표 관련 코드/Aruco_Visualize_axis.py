import cv2
import numpy as np
import time
import math

MARKER_LENGTH = 0.05  # 마커 한 변 길이 (단위: m)

# 캘리브레이션 데이터 불러오기
data = np.load("camera_calibration.npz")
camera_matrix = data["cameraMatrix"].astype(np.float32)
dist_coeffs = data["distCoeffs"].astype(np.float32)

def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("카메라를 열 수 없습니다.")
        return

    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, parameters)

    last_log_time = 0.0   # 마지막으로 로그 찍은 시각

    while True:
        ret, frame = cap.read()
        if not ret:
            print("프레임을 읽을 수 없습니다.")
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, rejected = detector.detectMarkers(gray)

        if ids is not None:
            cv2.aruco.drawDetectedMarkers(frame, corners, ids)

            # 현재 시간
            now = time.time()

            # 마지막 출력 후 1초 이상 지났을 때만 계산 + 출력
            if now - last_log_time >= 1.0:
                print("마커 ID:", ids.flatten())

                for corner in corners:
                    result = cv2.aruco.estimatePoseSingleMarkers(
                        [corner], MARKER_LENGTH, camera_matrix, dist_coeffs
                    )

                    if isinstance(result, tuple) and len(result) >= 2:
                        rvec, tvec = result[0], result[1]

                        r = rvec[0]  # (1,3)
                        t = tvec[0]  # (1,3)

                        # 위치 정보
                        tx, ty, tz = t[0][0], t[0][1], t[0][2]
                        tx_mm = tx * 1000.0
                        ty_mm = ty * 1000.0
                        tz_mm = tz * 1000.0

                        distance_m = math.sqrt(tx**2 + ty**2 + tz**2)
                        distance_mm = distance_m * 1000.0

                        print(f"[Position] x = {tx_mm:.1f} mm (오른쪽 +), "
                              f"y = {ty_mm:.1f} mm (아래 +), "
                              f"z = {tz_mm:.1f} mm (앞 +), "
                              f"거리 = {distance_mm:.1f} mm")

                        # 방향 정보 (roll, pitch, yaw)
                        R, _ = cv2.Rodrigues(r)

                        sy = math.sqrt(R[0, 0]**2 + R[1, 0]**2)
                        singular = sy < 1e-6

                        if not singular:
                            roll  = math.atan2(R[2, 1], R[2, 2])
                            pitch = math.atan2(-R[2, 0], sy)
                            yaw   = math.atan2(R[1, 0], R[0, 0])
                        else:
                            roll  = math.atan2(-R[1, 2], R[1, 1])
                            pitch = math.atan2(-R[2, 0], sy)
                            yaw   = 0

                        roll_deg  = math.degrees(roll)
                        pitch_deg = math.degrees(pitch)
                        yaw_deg   = math.degrees(yaw)

                        print(f"[Angle] roll  = {roll_deg:.1f} deg (x축 회전)")
                        print(f"        pitch = {pitch_deg:.1f} deg (y축 회전)")
                        print(f"        yaw   = {yaw_deg:.1f} deg (z축 회전)")
                        print("-" * 60)

                        # 좌표축 그리기 (이건 매 프레임 해도 됨)
                        cv2.drawFrameAxes(
                            frame,
                            camera_matrix,
                            dist_coeffs,
                            r,
                            t,
                            MARKER_LENGTH * 0.5,
                        )

                # 로그 찍은 시각 갱신
                last_log_time = now

        cv2.imshow("Aruco Detection (OpenCV 4.12)", frame)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break


    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
