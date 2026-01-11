import cv2
import numpy as np
import math
import sys  # 터미널 갱신용

MARKER_LENGTH = 0.05  # 마커 한 변 길이 (단위: m)

# 캘리브레이션 데이터 불러오기
data = np.load("camera_calibration.npz")
camera_matrix = data["cameraMatrix"].astype(np.float32)
dist_coeffs = data["distCoeffs"].astype(np.float32)

def print_status(text: str):
    """
    터미널에서 몇 줄짜리 상태 블록만 실시간으로 갱신하기 위한 함수.
    화면 전체를 지우고 커서를 맨 위로 올린 뒤 text를 출력한다.
    """
    # 전체 화면 지우기 + 커서를 (0,0)으로 이동
    sys.stdout.write("\x1b[2J\x1b[H")  # ESC[2J ESC[H
    sys.stdout.write(text)
    sys.stdout.flush()

def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("카메라를 열 수 없습니다.")
        return

    # ArUco 딕셔너리 및 탐지기 설정
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, parameters)

    print("실시간 Pose 출력 시작 (q 키로 종료)\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            print_status("프레임을 읽을 수 없습니다.")
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, rejected = detector.detectMarkers(gray)

        if ids is not None:
            # 마커 테두리 표시
            cv2.aruco.drawDetectedMarkers(frame, corners, ids)

            # 여기서는 '첫 번째 마커' 기준으로만 터미널에 출력
            corner = corners[0]
            marker_id = int(ids[0][0])

            result = cv2.aruco.estimatePoseSingleMarkers(
                [corner], MARKER_LENGTH, camera_matrix, dist_coeffs
            )

            # 결과 구조: (rvecs, tvecs, _)
            if isinstance(result, tuple) and len(result) >= 2:
                rvecs, tvecs = result[0], result[1]

                # rvecs, tvecs: (1, 1, 3) 형태 -> [0, 0]으로 벡터 하나 꺼냄
                rvec = rvecs[0][0]  # shape: (3,)
                tvec = tvecs[0][0]  # shape: (3,)

                # -----------------------------
                # 1) 위치 정보 (mm 단위)
                # -----------------------------
                tx, ty, tz = float(tvec[0]), float(tvec[1]), float(tvec[2])  # m 단위
                tx_mm = tx * 1000.0
                ty_mm = ty * 1000.0
                tz_mm = tz * 1000.0

                distance_m = math.sqrt(tx**2 + ty**2 + tz**2)
                distance_mm = distance_m * 1000.0

                # -----------------------------
                # 2) 방향 정보 (roll, pitch, yaw)
                # -----------------------------
                R, _ = cv2.Rodrigues(rvec)  # rvec -> 3x3 회전 행렬

                sy = math.sqrt(R[0, 0]**2 + R[1, 0]**2)
                singular = sy < 1e-6

                if not singular:
                    # x: roll, y: pitch, z: yaw (Tait-Bryan XYZ)
                    roll  = math.atan2(R[2, 1], R[2, 2])
                    pitch = math.atan2(-R[2, 0], sy)
                    yaw   = math.atan2(R[1, 0], R[0, 0])
                else:
                    # 특이 케이스
                    roll  = math.atan2(-R[1, 2], R[1, 1])
                    pitch = math.atan2(-R[2, 0], sy)
                    yaw   = 0.0

                roll_deg  = math.degrees(roll)
                pitch_deg = math.degrees(pitch)
                yaw_deg   = math.degrees(yaw)

                # -----------------------------
                # 3) 터미널에 여러 줄로 상태 출력 (매 프레임 갱신)
                # -----------------------------
                status = (
                    f"[마커 ID] {marker_id}\n"
                    f"[위치-카메라기준(mm)]\n"
                    f"  - X (오른쪽+)       :   {tx_mm:+7.1f} mm\n"
                    f"  - Y (아래+)         :   {ty_mm:+7.1f} mm\n"
                    f"  - Z (앞+)           :   {tz_mm:+7.1f} mm\n"
                    f"  - 카메라와의 거리   :   {distance_mm:7.1f} mm\n"
                    f"[자세(각도, deg)]\n"
                    f"  - 좌우 기울기 (roll)    :   {roll_deg:+6.1f}°\n"
                    f"  - 앞뒤 기울기 (pitch)   :   {pitch_deg:+6.1f}°\n"
                    f"  - 방향 회전   (yaw)     :   {yaw_deg:+6.1f}°\n"
                    f"\n(q 키를 누르면 종료됩니다)"
                )

                print_status(status)

                # 좌표축 그리기 (영상에서는 계속 갱신)
                cv2.drawFrameAxes(
                    frame,
                    camera_matrix,
                    dist_coeffs,
                    rvec,
                    tvec,
                    MARKER_LENGTH * 0.5,
                )
        else:
            # 마커 없으면 메시지 블록만
            status = "마커 없음\n\n(q 키를 누르면 종료됩니다)"
            print_status(status)

        cv2.imshow("Aruco Detection (OpenCV 4.12)", frame)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    print_status("종료합니다.\n")
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()