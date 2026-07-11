"""
실물 AGV 라파 설정 — 배포 시 보통 이 파일만 수정하면 됨
TU Capstone Design - AGV 물류 피킹 시스템

여기 있는 값은 두 라파가 '공유'하는 설정 (git 커밋 OK, 양쪽 동일).

※ '몇 번 AGV'(rid)는 라파마다 달라서 여기 두지 않음.
  → 라파별 ~/.bashrc 에 `export AGV_ID=1` (2번 라파는 2). (DISPLAY 옆에)
  (커밋 파일에 rid를 박으면 두 라파가 같은 값을 clone → '둘 다 1' 충돌/덮어쓰기)
"""

import os

# ─── 서버 (MQTT) ───
MQTT_HOST = "UB-Region5.local"   # PC 서버 mDNS 이름 (IP 바뀌어도 자동 해석). IP 직접 쓰려면 여기 교체
MQTT_PORT = 1883

TOPIC_CMD     = "/agv/cmd"      # 서버 → AGV 명령 (구독)
TOPIC_MARKER  = "/agv/marker"   # AGV → 서버 위치 보고 (발행)
TOPIC_CMD_ACK = "/agv/cmd_ack"  # AGV → 서버 명령 완료 (발행)

# ─── UART (STM32) ───
UART_PORT    = "/dev/ttyAMA10"  # 주원이 카메라 코드와 동일 포트
UART_BAUD    = 115200
UART_ENABLED = True             # 실물 AGV 전용 — UART 활성 (SIL 테스트는 monkeypatch로 덮음)

# ─── 카메라 ───
# 캘리브레이션 파일은 hardware/ 폴더 기준으로 해석 (cwd 무관)
CALIB_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_calibration.pkl")
SHOW_PREVIEW = True             # 헤드리스(디스플레이 없는) 라파면 False
