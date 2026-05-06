"""
RPi AGV 진입점
TU Capstone Design - AGV 물류 피킹 시스템

실행:
  python3 rpi_main.py <rid>
  예) python3 rpi_main.py 1   (AGV-1)
      python3 rpi_main.py 2   (AGV-2)

실물 전환 시:
  1. bridge.py 상단 UART_ENABLED = True 로 변경
  2. camera_calibration.pkl 파일 준비
"""

import sys
import time

from hardware.bridge import Bridge
from hardware.camera import RpiCamera


def main():
    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"[RPi AGV-{rid}] 시작")

    # ─── Bridge 초기화 ────────────────────────────────────────────────────────
    bridge = Bridge(rid=rid)  # cmd_handler=None → UART 모드
    bridge.open_uart()        # UART 포트 열기 + 수신 스레드 시작
    bridge.connect()          # MQTT 연결

    # ─── Camera 초기화 ────────────────────────────────────────────────────────
    try:
        camera = RpiCamera("camera_calibration.pkl")
        print(f"[RPi AGV-{rid}] 카메라 초기화 완료")
    except Exception as e:
        print(f"[RPi AGV-{rid}] 카메라 초기화 실패: {e}")
        camera = None

    # ─── 메인 루프 ────────────────────────────────────────────────────────────
    # 카메라로 ArUco 마커 감지 → /agv/marker 발행
    # UART 수신은 bridge 내부 스레드가 처리 (ROTATE_DONE / LIFT_DONE → cmd_ack)
    print(f"[RPi AGV-{rid}] 실행 중... Ctrl+C로 종료")
    try:
        while True:
            if camera:
                marker_id, heading_deg = camera.detect()
                if marker_id is not None:
                    bridge.publish_marker(marker_id, heading_deg)
            time.sleep(0.05)  # 20Hz
    except KeyboardInterrupt:
        print(f"\n[RPi AGV-{rid}] 종료 중...")
    finally:
        if camera:
            camera.release()
        bridge.disconnect()


if __name__ == "__main__":
    main()
