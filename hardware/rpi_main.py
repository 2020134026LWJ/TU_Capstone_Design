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
  --preview : 웹 프리뷰 (http://<라파IP>:8000) — 헤드리스 라파를 PC 브라우저로 관찰 (기본 꺼짐)

배포 설정은 hardware/config.py 만 수정 (MQTT_HOST, UART_*, CALIB_FILE, SHOW_PREVIEW).
  · camera_calibration.pkl 은 hardware/ 에 두면 자동으로 찾음 (config.CALIB_FILE)
  · AGV 라파가 헤드리스(디스플레이 없음)면 config.SHOW_PREVIEW = False

[TODO 미팅] heading 출처 — 옵션 a로 heading 미전송(서버가 경로 기반 계산). 절대방위 필요 시 STM IMU.
"""

import argparse
import os
import time

from hardware.bridge_rpi import Bridge
from hardware.camera import RpiCamera, yaw_to_heading
from hardware.config import CALIB_FILE, SHOW_PREVIEW   # 배포 설정은 config.py만 수정


def _resolve_rid(arg_rid=None) -> int:
    """자기가 몇 번 AGV인지 결정.
      1) 명령행 인자 (python3 -m hardware.rpi_main 2)  — 명시 우선
      2) 환경변수 AGV_ID (라파별 .bashrc에 export AGV_ID=1/2)  — 디바이스 고정
      3) 둘 다 없으면 에러 (기본 1 금지 — 두 라파가 같은 코드라 '둘 다 1' 충돌 방지)
    """
    if arg_rid is not None:
        return arg_rid
    env = os.environ.get("AGV_ID")
    if env:
        return int(env)
    raise SystemExit(
        "AGV id 미지정 — `python3 -m hardware.rpi_main 1`(또는 2) 로 주거나 "
        "라파 .bashrc에 `export AGV_ID=1`(2번 라파는 2)을 추가하세요."
    )


def main():
    p = argparse.ArgumentParser(description="RPi AGV (실물) — bridge + camera")
    p.add_argument("rid", nargs="?", type=int, default=None,
                   help="AGV 번호 (없으면 env AGV_ID 사용)")
    p.add_argument("--preview", action="store_true",
                   help="MJPEG 웹 프리뷰 (http://<라파IP>:8000). 헤드리스 라파를 PC 브라우저로 관찰")
    p.add_argument("--preview-port", type=int, default=8000)
    args = p.parse_args()

    rid = _resolve_rid(args.rid)
    print(f"[RPi AGV-{rid}] 시작")

    # ─── Bridge (UART 다리) ───────────────────────────────────────────────────
    bridge = Bridge(rid=rid)
    bridge.open_uart()        # UART 포트 열기 + 수신 스레드 (STM DONE/ACK → cmd_ack)
    bridge.connect()          # 서버 MQTT 연결

    # ─── Camera (ArUco 비전, 주원이 opencv 기반) ──────────────────────────────
    try:
        camera = RpiCamera(CALIB_FILE, show_preview=SHOW_PREVIEW, keep_frame=args.preview)
        print(f"[RPi AGV-{rid}] 카메라 초기화 완료")
    except Exception as e:
        print(f"[RPi AGV-{rid}] 카메라 초기화 실패: {e}")
        camera = None

    # ─── (선택) 웹 프리뷰 — 카메라 주인이 곧 rpi_main이므로 여기서 스트리밍한다 ───
    # (별도 camera_preview.py는 카메라를 또 열어 충돌 → --preview로 rpi_main 안에서 켠다)
    preview = None
    preview_out = None
    if args.preview:
        from hardware import mjpeg_preview as preview
        import subprocess
        preview_out = preview.start_preview_server(
            args.preview_port, note="rpi_main 실행 중 — 화면의 값 = 서버로 가는 값 그대로")
        _ip = subprocess.run(['hostname', '-I'], capture_output=True, text=True).stdout.split()
        print(f"[RPi AGV-{rid}] 웹 프리뷰 → http://{_ip[0] if _ip else '<라파IP>'}:{args.preview_port}")

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

                if preview_out is not None and camera.last_frame is not None:
                    preview.draw_hud(camera.last_frame, marker_id, x_mm, y_mm, yaw_deg)
                    preview.push_frame(preview_out, camera.last_frame)

                if marker_id is not None:
                    bridge.set_marker_offset(x_mm, y_mm, yaw_deg)   # STM: PID offset (UART_OFFSET_HZ 상한)
                    bridge.publish_pose(marker_id, x_mm, y_mm, yaw_deg)  # 트윈: 연속 자세 (수정 68)
                    if marker_id != prev_marker:                    # 서버: 새 마커일 때만 (시뮬과 동일)
                        # 수정 69(A 배관): heading을 '관찰용'으로만 보낸다.
                        # HEADING_OFFSET이 실측 전이라 서버는 비교/로그만 하고 제어엔 안 쓴다.
                        bridge.publish_marker(marker_id,
                                              heading_observed=yaw_to_heading(yaw_deg))
                        prev_marker = marker_id
            time.sleep(0.05)  # 20Hz
    except KeyboardInterrupt:
        print(f"\n[RPi AGV-{rid}] 종료 중...")
    finally:
        # 카메라를 놓기 전에 리프트를 내린다 — offset 스트림이 살아 있어야 STM PID가 안정적.
        try:
            bridge.park_lift()
        except Exception as e:
            print(f"[RPi AGV-{rid}] 리프트 정리 실패(무시): {e}")
        if camera:
            camera.release()
        bridge.disconnect()


if __name__ == "__main__":
    main()
