"""
RPi AGV 진입점 (실물)
TU Capstone Design - AGV 물류 피킹 시스템

역할:
  bridge_rpi(UART 다리) + camera(ArUco 비전, 주원이 opencv 기반)를 한 프로세스에서 엮음.
  - camera.detect() → (marker_id, x_mm, y_mm, yaw_deg)
  - bridge.set_marker_offset(x, y, yaw) : offset을 UART 패킷 <cmd,x,y,yaw>에 합쳐 STM에 전달
  - bridge.publish_marker(id, heading)  : marker_id를 서버에 보고 (위치 추적)
  - bridge : 서버 /agv/cmd 수신 → UART 전달 / STM DONE·ACK → /agv/cmd_ack

실행:
  python3 -m hardware.rpi_main 1   (AGV-1)
  python3 -m hardware.rpi_main 2   (AGV-2)

실물 전환 체크:
  1. bridge_rpi.py 상단 UART_ENABLED = True, MQTT_HOST = "서버IP"
  2. UART 포트 /dev/ttyAMA10 (주원이 카메라와 동일)
  3. camera_calibration.pkl 준비

[TODO 미팅] camera의 yaw_deg(0~360) → 서버 heading(0=N/90=E) 변환 규약 (지금은 yaw 그대로 발행)
"""

import sys
import time

from hardware.bridge_rpi import Bridge
from hardware.camera import RpiCamera


def main():
    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"[RPi AGV-{rid}] 시작")

    # ─── Bridge (UART 다리) ───────────────────────────────────────────────────
    bridge = Bridge(rid=rid)
    bridge.open_uart()        # UART 포트 열기 + 수신 스레드 (STM DONE/ACK → cmd_ack)
    bridge.connect()          # 서버 MQTT 연결

    # ─── Camera (ArUco 비전, 주원이 opencv 기반) ──────────────────────────────
    try:
        camera = RpiCamera("camera_calibration.pkl")
        print(f"[RPi AGV-{rid}] 카메라 초기화 완료")
    except Exception as e:
        print(f"[RPi AGV-{rid}] 카메라 초기화 실패: {e}")
        camera = None

    # ─── 메인 루프 ────────────────────────────────────────────────────────────
    # 카메라 감지 → offset은 bridge로(UART 패킷에 합성), marker_id는 서버로 발행
    print(f"[RPi AGV-{rid}] 실행 중... Ctrl+C로 종료")
    try:
        while True:
            if camera:
                marker_id, x_mm, y_mm, yaw_deg = camera.detect()
                if marker_id is not None:
                    bridge.set_marker_offset(x_mm, y_mm, yaw_deg)  # offset → UART 패킷
                    bridge.publish_marker(marker_id, yaw_deg)       # heading=yaw (TODO 변환)
            time.sleep(0.05)  # 20Hz
    except KeyboardInterrupt:
        print(f"\n[RPi AGV-{rid}] 종료 중...")
    finally:
        if camera:
            camera.release()
        bridge.disconnect()


if __name__ == "__main__":
    main()
