"""
RPi AGV 진입점 (실물)
TU Capstone Design - AGV 물류 피킹 시스템

역할:
  bridge_rpi(UART 다리) + camera(ArUco 비전, 주원이 opencv 기반)를 한 프로세스에서 엮음.
  - camera.detect() → (marker_id, x_mm, y_mm, yaw_deg)
  - bridge.set_marker_offset(x, y, yaw) : offset을 UART 패킷 <cmd,x,y,yaw>에 합쳐 STM에 전달
  - bridge.publish_marker(id, heading)  : marker_id를 서버에 보고 (위치 추적)
  - bridge : 서버 /agv/cmd 수신 → UART 전달 / STM DONE·ACK → /agv/cmd_ack

실행 (clone 후 repo 루트에서):
  python3 -m hardware.rpi_main 1   (AGV-1)   # 인자로 id 지정
  python3 -m hardware.rpi_main 2   (AGV-2)
  ※ 또는 라파별 .bashrc에 `export AGV_ID=1`(2번 라파는 2) → 인자 없이 실행 가능
    (인자·AGV_ID 둘 다 없으면 에러 — '둘 다 1' 충돌 방지)

배포 설정은 hardware/config.py 만 수정 (MQTT_HOST, UART_*, CALIB_FILE, SHOW_PREVIEW).
  · camera_calibration.pkl 은 hardware/ 에 두면 자동으로 찾음 (config.CALIB_FILE)
  · AGV 라파가 헤드리스(디스플레이 없음)면 config.SHOW_PREVIEW = False

[TODO 미팅] heading 출처 — 옵션 a로 heading 미전송(서버가 경로 기반 계산). 절대방위 필요 시 STM IMU.
"""

import os
import sys
import time

from hardware.bridge_rpi import Bridge
from hardware.camera import RpiCamera
from hardware.config import CALIB_FILE, SHOW_PREVIEW   # 배포 설정은 config.py만 수정


def _resolve_rid() -> int:
    """자기가 몇 번 AGV인지 결정.
      1) 명령행 인자 (python3 -m hardware.rpi_main 2)  — 명시 우선
      2) 환경변수 AGV_ID (라파별 .bashrc에 export AGV_ID=1/2)  — 디바이스 고정
      3) 둘 다 없으면 에러 (기본 1 금지 — 두 라파가 같은 코드라 '둘 다 1' 충돌 방지)
    """
    if len(sys.argv) > 1:
        return int(sys.argv[1])
    env = os.environ.get("AGV_ID")
    if env:
        return int(env)
    raise SystemExit(
        "AGV id 미지정 — `python3 -m hardware.rpi_main 1`(또는 2) 로 주거나 "
        "라파 .bashrc에 `export AGV_ID=1`(2번 라파는 2)을 추가하세요."
    )


def main():
    rid = _resolve_rid()
    print(f"[RPi AGV-{rid}] 시작")

    # ─── Bridge (UART 다리) ───────────────────────────────────────────────────
    bridge = Bridge(rid=rid)
    bridge.open_uart()        # UART 포트 열기 + 수신 스레드 (STM DONE/ACK → cmd_ack)
    bridge.connect()          # 서버 MQTT 연결

    # ─── Camera (ArUco 비전, 주원이 opencv 기반) ──────────────────────────────
    try:
        camera = RpiCamera(CALIB_FILE, show_preview=SHOW_PREVIEW)
        print(f"[RPi AGV-{rid}] 카메라 초기화 완료")
    except Exception as e:
        print(f"[RPi AGV-{rid}] 카메라 초기화 실패: {e}")
        camera = None

    # ─── 메인 루프 ────────────────────────────────────────────────────────────
    # 카메라 감지 → offset은 bridge로(UART 패킷에 합성), marker_id는 서버로 발행
    print(f"[RPi AGV-{rid}] 실행 중... Ctrl+C로 종료")
    # 서버 발행용 직전 마커 (시뮬 IsaacCamera._last_marker와 동일 의미).
    # None으로 리셋 안 함 → 떠나는 노드 재감지·중복 트리거 방지.
    prev_marker = None
    try:
        while True:
            if camera:
                marker_id, x_mm, y_mm, yaw_deg = camera.detect()
                if marker_id is not None:
                    bridge.set_marker_offset(x_mm, y_mm, yaw_deg)   # STM: 매 프레임 (PID offset, yaw 포함)
                    if marker_id != prev_marker:                    # 서버: 새 마커일 때만 (시뮬과 동일)
                        # heading 미전송(옵션 a) → 서버가 경로 기반 계산. 카메라 yaw는 STM에만.
                        bridge.publish_marker(marker_id)
                        prev_marker = marker_id
            time.sleep(0.05)  # 20Hz
    except KeyboardInterrupt:
        print(f"\n[RPi AGV-{rid}] 종료 중...")
    finally:
        if camera:
            camera.release()
        bridge.disconnect()


if __name__ == "__main__":
    main()
