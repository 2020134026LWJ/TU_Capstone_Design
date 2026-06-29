from picamera2 import Picamera2
import cv2
import numpy as np
import time
import pickle
import serial

CMD_FORWARD       = 0x01
CMD_STOP          = 0x02
CMD_LIFT_UP       = 0x03
CMD_LIFT_DOWN     = 0x04
CMD_ROTATE_LEFT   = 0x05    
CMD_ROTATE_RIGHT  = 0x06
CMD_ROTATE_180    = 0x07

EVT_DONE          = 0x81  # 동작 완료
EVT_ACK           = 0xFF  # 명령 수신 확인

def live_aruco_detection(calibration_data):
    """
    실시간으로 비디오를 받아 ArUco 마커를 검출하고 3D 포즈를 추정하는 함수
    
    Args:
        calibration_data: 카메라 캘리브레이션 데이터를 포함한 딕셔너리
            - camera_matrix: 카메라 내부 파라미터 행렬
            - dist_coeffs: 왜곡 계수
    """
    # 캘리브레이션 데이터 추출
    camera_matrix = calibration_data['camera_matrix']
    dist_coeffs = calibration_data['dist_coeffs']
    
    # ArUco 검출기 설정
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    aruco_params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, aruco_params)

    # 마커 크기 및 3D 좌표 설정 (mm 단위)
    marker_size = 25
    # 마커 중심을 원점으로 하고, 네 꼭짓점의 위치를 정의
    half = marker_size / 2
    marker_3d_edges = np.array([
        [-half, half, 0],               # topLeft
        [half, half, 0],                # topRight
        [half, -half, 0],               # bottomRight
        [-half, -half, 0]               # bottomLeft
    ], dtype='float32') 
    
    # 카메라 초기화
    picam2 = Picamera2()
    camera_config = picam2.create_preview_configuration()
    picam2.configure(camera_config)
    picam2.start()

    # usart pin8, 10 설정
    uart = serial.Serial('/dev/ttyAMA10', baudrate=115200, timeout=1)
    
    print("카메라 시작됨 (q 키로 종료)")

    # 카메라 초기화 대기
    time.sleep(2)

    cv2.namedWindow('ArUco Marker Detection', cv2.WND_PROP_FULLSCREEN)
    cv2.setWindowProperty('ArUco Marker Detection', cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    
    command = 0x00

    try:
        while True:
            if uart.in_waiting > 0:
                rx_data = uart.read(1)
                if rx_data[0] == EVT_DONE:
                    print("EVT_DONE - 다음 명령 입력:")
                    cmd_str = input("> ")
                    command = int(cmd_str)
                    print(hex(rx_data[0]))
                elif rx_data[0] == EVT_ACK:
                    print("EVT_ACK")
                    command = 0x00
                    print(hex(rx_data[0]))

            # 프레임 읽기
            frame = picam2.capture_array()

            # BGR를 RGB로 변환
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
            # 이미지 왜곡 보정 (캘리브레이션 적용)
            frame_undistorted = cv2.undistort(frame_rgb, camera_matrix, dist_coeffs)
            
            # 마커 검출
            corners, ids, rejected = detector.detectMarkers(frame_undistorted)
            
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
                    cv2.circle(frame_undistorted, topLeftPoint, 4, (255, 0, 0), -1)
                    cv2.circle(frame_undistorted, topRightPoint, 4, (255, 0, 0), -1)
                    cv2.circle(frame_undistorted, bottomRightPoint, 4, (255, 0, 0), -1)
                    cv2.circle(frame_undistorted, bottomLeftPoint, 4, (255, 0, 0), -1)
                
                    # PnP로 포즈 추정
                    ret, rvec, tvec = cv2.solvePnP(
                        marker_3d_edges, 
                        corner, 
                        camera_matrix, 
                        dist_coeffs
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
                    corner = corners[i][0]
                    pos = (int(topLeft[0]), int(topLeft[1]) - 10)
                    text = f"ID:{ids[i][0]} ({x}, {y})mm {yaw_deg}deg"
                    cv2.putText(frame_undistorted, text, pos,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                    
                    # 좌표축 표시
                    cv2.drawFrameAxes(
                        frame_undistorted,      # 이미지
                        camera_matrix,          # 카메라 내부 파라미터 행렬
                        dist_coeffs,            # 카메라 왜곡 계수
                        rvec,                   # 회전 벡터
                        tvec,                   # 평행이동 벡터
                        marker_size / 2         # 축의 길이 (mm 단위)
                    )

                    # 각도 전송
                    # Write a string to UART Serial Port
                    msg = f"<{int(command)},{int(x*10):+05d},{int(y*10):+05d},{int(yaw_deg*10):+05d}>"
                    uart.write(msg.encode())
            
            # 프레임 표시
            cv2.imshow('ArUco Marker Detection', frame_undistorted)
            
            # 'q' 키를 누르면 종료
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    
    except KeyboardInterrupt:
        print("\n종료 중...")
    
    finally:
        # 리소스 해제
        picam2.stop()
        cv2.destroyAllWindows()
        print("카메라 종료됨")

def main():
    # 캘리브레이션 데이터 로드
    try:
        with open('camera_calibration.pkl', 'rb') as f:
            calibration_data = pickle.load(f)
        print("캘리브레이션 데이터 로드 성공")
    except FileNotFoundError:
        print("Error: camera_calibration.pkl 파일을 찾을 수 없습니다!")
        print("먼저 캘리브레이션을 수행하세요.")
        return
    except Exception as e:
        print(f"캘리브레이션 데이터 로드 에러: {e}")
        return
    
    print("ArUco 마커 검출 시작...")
    live_aruco_detection(calibration_data)

if __name__ == "__main__":
    main()
