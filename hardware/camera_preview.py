"""카메라 웹 프리뷰 (단독 셋업 도구) — 라파에서 실행, PC 브라우저에서 본다.

라파에 모니터가 없어도 카메라 화면을 볼 수 있다. MJPEG로 스트리밍하므로
브라우저만 있으면 된다 (X11 포워딩 불필요).

    # 라파에서
    python3 -m hardware.camera_preview

    # PC 브라우저에서
    http://<라파IP>:8000

[중요] 검출은 **hardware/camera.py(RpiCamera)를 그대로 사용**한다. 프리뷰가 따로
계산하면 화면에 보이는 숫자와 실제로 서버/STM으로 가는 숫자가 갈릴 수 있다.
여기 뜨는 값 = 시스템이 쓰는 값.

용도 1 — 초점 맞추기:
  `sharpness`(라플라시안 분산)가 **최대**가 되는 지점이 초점이다. ov5647은 수동 초점이라
  렌즈를 손으로 돌리며 숫자를 본다. 눈대중보다 정확하다.

용도 2 — yaw 규약 측정 (heading 변환 상수 확정):
  ★★ 마커 카드를 **책상에 놓고, 카메라를 손으로 돌린다.** 카드를 돌리면 안 된다.
  카메라는 '카메라와 마커의 상대 각도'만 재고 **누가 돌았는지 구분하지 못한다.**
  실제로는 마커가 바닥에 붙박이고 로봇(카메라)이 도는데, 반대로 카드를 돌려 측정하면
  **부호가 뒤집힌 채로 규약이 굳는다** → 실물에서 로봇이 반대로 돈다.
  (회전의자에 앉아 오른쪽으로 돌면 방이 왼쪽으로 도는 것처럼 보이는 것과 같다)

[주의] 카메라를 독점한다. run_bench / rpi_main 과 동시 실행 불가.
       rpi_main 돌리는 중에 화면을 보려면 → `python3 -m hardware.rpi_main --preview`
       (그쪽은 카메라를 하나만 열고 같은 MJPEG 서버로 스트리밍한다).
"""

import time

import cv2

from hardware.camera import RpiCamera
from hardware.config import CALIB_FILE
from hardware.mjpeg_preview import PORT, push_frame, start_preview_server

_NOTE = ("<b>초점</b>: sharpness 가 <b>최대</b>가 되도록 렌즈를 돌리세요.<br>"
         "<b>yaw 측정</b>: 마커를 <b>책상에 놓고 카메라를 돌리세요</b> "
         "(카드를 돌리면 부호가 반대로 나옵니다).")


def main():
    # 실제 운용과 같은 검출기 — 화면의 숫자 = 서버/STM으로 가는 숫자
    camera = RpiCamera(CALIB_FILE, show_preview=False, keep_frame=True)

    output = start_preview_server(PORT, note=_NOTE)

    import subprocess
    ip = subprocess.run(['hostname', '-I'], capture_output=True, text=True).stdout.split()
    ip = ip[0] if ip else '<라파IP>'
    print(f"\n  브라우저에서 열기 →  http://{ip}:{PORT}\n")
    print("  [초점]   sharpness 가 최대가 되도록 렌즈를 돌리세요.")
    print("  [yaw]    ★마커는 책상에 놓고 **카메라를 돌리세요** (카드를 돌리면 부호가 반대).")
    print("           카메라를 시계방향으로 천천히 90° 돌리며 yaw 를 보세요.\n")

    last_log = 0.0
    try:
        while True:
            mid, x, y, yaw = camera.detect()
            frame = camera.last_frame
            if frame is None:
                time.sleep(0.03)
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()

            color = (0, 255, 0) if sharpness > 100 else (0, 0, 255)
            cv2.putText(frame, f"sharpness {sharpness:6.0f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

            if mid is None:
                cv2.putText(frame, "no marker", (10, 70),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (128, 128, 128), 2)
            else:
                # 큰 글씨로 yaw — 카메라를 돌리며 이 값이 어디로 가는지 본다
                cv2.putText(frame, f"yaw {yaw:7.1f}", (10, 80),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 255), 3)
                cv2.putText(frame, f"ID {mid}   x {x:.0f}mm  y {y:.0f}mm", (10, 120),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

                # 콘솔에도 — 회전시키는 동안의 변화를 기록으로 남긴다
                if time.time() - last_log > 0.4:
                    print(f"  ID {mid}  yaw {yaw:7.1f}°   x {x:7.1f}mm  y {y:7.1f}mm   "
                          f"sharp {sharpness:5.0f}")
                    last_log = time.time()

            push_frame(output, frame)
            time.sleep(0.03)
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        camera.release()


if __name__ == '__main__':
    main()
