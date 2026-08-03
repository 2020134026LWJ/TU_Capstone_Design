"""STM32 디버그 로그 수집기 (USART3 → ST-Link VCP)
TU Capstone Design - AGV 물류 피킹 시스템

주원이 펌웨어(main.c:149)는 printf를 USART3(PD8/PD9)로 리타겟해 뒀고,
NUCLEO-F767ZI는 USART3가 ST-Link 가상 COM 포트에 연결돼 있다.
→ 펌웨어 구울 때 쓰는 USB 케이블 그대로 꽂으면 /dev/ttyACM0 으로 로그가 나온다.
  (펌웨어 수정·추가 부품 불필요. printf는 원래도 나가고 있으므로 관측이 거동을 안 바꾼다.)

쓰임: rpi_seq 로그(파이썬이 STM에 무엇을 보냈나)와 이 로그(STM이 그걸로 무엇을 했나)를
      시각으로 맞춰 보기 위한 것. 그래서 매 줄에 벽시계 타임스탬프를 붙인다.

실행:
    python3 tools/stm_log.py                    # 화면 + ~/stm_MMDD_HHMM.log 저장
    python3 tools/stm_log.py --port /dev/ttyACM1
    python3 tools/stm_log.py --grep             # 관심 줄(dyaw/RX FAIL/target)만

보는 법:
    RX FAIL sz=...        → 패킷 프레이밍 실패 (torn-read/뭉침이 실재한다는 증거)
    dyaw: N, ang: M       → 회전 명령 수신 시점. |dyaw|>=5 면 Target_Yaw 보정이 개입한 것
    x_mm/y_mm/ang/dist    → forward 명령 수신 시점의 목표
    target/yaw/L_spd/...  → 주행·회전 중 실시간 추종
"""

import argparse
import os
import sys
import time

import serial

# 관심 줄만 볼 때 쓰는 키워드 (--grep)
KEYS = ("RX FAIL", "dyaw", "x_mm", "UPDATE YAW", "Main Program", "Calibration",
        "GLITCH")   # 주원 2026-07-26: [YAW GLITCH] / [MARKER GLITCH] 가드 추가분


def main():
    p = argparse.ArgumentParser(description="STM32 USART3 디버그 로그 수집 (ST-Link VCP)")
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--grep", action="store_true", help="관심 줄만 (dyaw/RX FAIL/x_mm 등)")
    p.add_argument("--out", default=None, help="저장 경로 (기본 ~/stm_MMDD_HHMM.log)")
    args = p.parse_args()

    out_path = args.out or os.path.expanduser(
        time.strftime("~/stm_%m%d_%H%M.log").replace("~", os.path.expanduser("~")))

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except Exception as e:
        print(f"포트 열기 실패 {args.port}: {e}", file=sys.stderr)
        print("  ls /dev/ttyACM*  로 포트 확인 / 권한은 dialout 그룹", file=sys.stderr)
        return 1

    print(f"[stm_log] {args.port}@{args.baud} 수집 시작 → {out_path}")
    print("[stm_log] 아무것도 안 나오면 STM 리셋 버튼을 눌러보세요 "
          "('--- AGV Main Program Start ---' 가 떠야 정상)")

    buf = b""
    with open(out_path, "a", buffering=1) as f:
        try:
            while True:
                d = ser.read(512)
                if not d:
                    continue
                buf += d
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    now = time.time()
                    stamp = f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now * 1000) % 1000:03d}"
                    rec = f"[{stamp}] {line}"
                    f.write(rec + "\n")
                    if not args.grep or any(k in line for k in KEYS):
                        print(rec, flush=True)
        except KeyboardInterrupt:
            print(f"\n[stm_log] 종료 — 저장됨: {out_path}")
        finally:
            ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
