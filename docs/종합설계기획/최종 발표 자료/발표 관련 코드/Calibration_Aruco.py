import cv2
import numpy as np
import glob

# 체스보드 패턴 크기 (내가 인쇄한 체스보드의 내부 코너 수)
CHESSBOARD_SIZE = (9, 6)

# 3D 공간상의 기준점 생성 (Z=0인 평면)
objp = np.zeros((CHESSBOARD_SIZE[0] * CHESSBOARD_SIZE[1], 3), np.float32)
objp[:, :2] = np.mgrid[0:CHESSBOARD_SIZE[0], 0:CHESSBOARD_SIZE[1]].T.reshape(-1, 2)

# 포인트 저장용 리스트
objpoints = []  # 실제 3D 점
imgpoints = []  # 영상 2D 점

# 카메라 열기
cap = cv2.VideoCapture(0)
print("카메라 캘리브레이션 시작. 체스보드를 여러 각도에서 보여주세요.")
print("s를 눌러 저장, q로 종료")

while True:
    ret, frame = cap.read()
    if not ret:
        break

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    ret_corners, corners = cv2.findChessboardCorners(gray, CHESSBOARD_SIZE, None)

    if ret_corners:
        cv2.drawChessboardCorners(frame, CHESSBOARD_SIZE, corners, ret_corners)

    cv2.imshow('Calibration', frame)
    key = cv2.waitKey(1) & 0xFF

    if key == ord('s') and ret_corners:
        print("체스보드 이미지 저장!")
        objpoints.append(objp)
        imgpoints.append(corners)
    elif key == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()

# 최소 10장 이상 저장했을 때만 실행
if len(objpoints) > 5:
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(objpoints, imgpoints, gray.shape[::-1], None, None)

    print("\n=== 캘리브레이션 결과 ===")
    print("Camera Matrix:\n", mtx)
    print("Distortion Coeffs:\n", dist.ravel())

    # 결과를 파일로 저장
    np.savez("camera_calibration.npz", cameraMatrix=mtx, distCoeffs=dist)
    print("\n결과 저장 완료 camera_calibration.npz")
else:
    print("충분한 샘플이 없습니다. 6장 이상 촬영하세요.")
