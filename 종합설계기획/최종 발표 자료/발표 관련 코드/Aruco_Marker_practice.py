import cv2

def main():
    # 1) 웹캠 열기 (0은 기본 카메라)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("카메라를 열 수 없습니다.")
        return

    # 2) 사용할 ArUco 딕셔너리 선택
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    parameters = cv2.aruco.DetectorParameters()  # 기본 파라미터

    # 3) 새 버전 OpenCV 스타일의 Detector 객체
    detector = cv2.aruco.ArucoDetector(aruco_dict, parameters)  # :contentReference[oaicite:1]{index=1}

    while True:
        ret, frame = cap.read()
        if not ret:
            print("프레임을 읽을 수 없습니다.")
            break

        # 그레이스케일 변환 (필수는 아니지만 일반적으로 이렇게 함)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # 4) 마커 검출
        corners, ids, rejected = detector.detectMarkers(gray)

        # 5) 검출된 마커를 그림 위에 표시
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(frame, corners, ids)

            # 각 마커의 중심 좌표와 ID 찍기
            for corner, marker_id in zip(corners, ids.flatten()):
                pts = corner[0]  # 4개의 꼭짓점 (x,y)
                # 중심점 계산
                cX = int(pts[:, 0].mean())
                cY = int(pts[:, 1].mean())

                cv2.circle(frame, (cX, cY), 5, (0, 0, 255), -1)
                cv2.putText(frame, f"ID: {marker_id}", (cX - 20, cY - 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # 6) 화면 출력
        cv2.imshow("Aruco Detection", frame)

        # q 누르면 종료
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
