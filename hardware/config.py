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

# 수정 68 (B) — 연속 자세 스트림. **트윈 전용, 서버는 구독하지 않는다.**
#
# 마커/cmd_ack는 '띄엄띄엄한 이벤트'라 트윈은 그 사이를 시간으로 보간할 수밖에 없었다.
# 그런데 **회전은 노드 위에서 제자리로 도니 발밑 마커가 계속 보인다** → 추정이 아니라
# **측정**이 가능하다. (직진은 마커가 시야를 벗어나 보간 불가피 — 이 비대칭이 핵심.)
# 카메라가 이미 매 프레임 (x, y, yaw)를 계산해 STM으로만 보내고 있으므로, MQTT로 한 줄 더
# 발행하면 된다. 엔코더도 STM 펌웨어 수정도 필요 없다.
#
# 서버는 이 토픽을 안 본다 — 연속 위치가 경로계획/충돌회피를 흔들면 안 된다.
# 라파 부하: MQTT publish 16.9µs vs camera.detect() 53ms = 0.03%. (실측)
TOPIC_POSE     = "/agv/pose"
POSE_PUBLISH_HZ = 20            # 발행 주기 상한 (카메라가 더 빨라도 이 이상 안 나감)

# ─── UART (STM32) ───
UART_PORT    = "/dev/ttyAMA10"  # 주원이 카메라 코드와 동일 포트
UART_BAUD    = 115200
UART_ENABLED = True             # 실물 AGV 전용 — UART 활성 (SIL 테스트는 monkeypatch로 덮음)

# STM 오프셋 스트리밍 주기 상한 [Hz] (수정 67)
#
# 왜 필요한가: 예전엔 카메라가 한 프레임 볼 때마다 UART 패킷을 STM에 쐈다.
# 즉 **UART 전송 주기 = 카메라 프레임률**로 우연히 묶여 있었다. 그래서 카메라 코드를
# 최적화하면(예: undistort 제거) STM이 받는 패킷 수가 **조용히 따라 올라간다** —
# 아무도 의도하지 않았는데. 카메라 성능을 건드릴 때마다 STM 부하를 걱정해야 하는 구조였다.
#
# 이 값으로 상한을 걸어 둘을 분리한다. 카메라가 10fps든 30fps든 STM은 항상 이 주기로 받는다.
# 10 = 지금 실제 주기 (detect 53ms + sleep 50ms ≈ 103ms). 즉 **현 동작 그대로 유지.**
# 명령(turn/lift/forward)은 이 제한을 받지 않는다 — 즉시 나가야 한다.
UART_OFFSET_HZ = 10

# 수정 69 (A 배관) — 카메라 yaw → 서버 heading 변환
#
# 실측 확정 (2026-07-12): **카메라(로봇)가 시계방향으로 돌면 yaw가 증가한다. 1:1 스케일.**
# 서버 heading(0=N/90=E/180=S/270=W)도 나침반 = 시계방향이 + → **부호가 같다.** 덧셈이면 된다.
#   heading = (yaw + HEADING_OFFSET) % 360
#
# HEADING_OFFSET = "yaw가 0일 때 로봇이 실제로 보는 방향".
# 카메라 장착 각도 + 바닥 마커를 어느 방향으로 깔았는지로 정해진다 → **실물에서 1회 실측.**
#   측정법: AGV를 홈 노드에 **북쪽 보게** 놓고 yaw를 읽는다 → HEADING_OFFSET = (0 - yaw) % 360
#   ⚠️전제: 바닥 마커를 **전부 같은 방향으로** 깔아야 한다. 한 장이라도 돌아가면 그 노드만 틀어진다.
#
# **0 = 주원이가 항상 0으로 맞추겠다고 한 값** (2026-07-12). 즉 자리채우기가 아니라 합의값.
# 다만 아직 실물로 확인한 적은 없으므로 밸브는 잠가둔다
# (server.config.TRUST_CAMERA_HEADING = False → 비교/로그만).
# 실물 주행에서 서버 로그가 `[heading] ... 차이 0°`로 나오면(=경고가 안 뜨면) 확인된 것 →
# 그때 TRUST_CAMERA_HEADING = True 로 열면 끝. 검증이 '로그 읽기' 한 번으로 끝난다.
HEADING_OFFSET = 0

# ─── 카메라 ───
# 캘리브레이션 파일은 hardware/ 폴더 기준으로 해석 (cwd 무관)
CALIB_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_calibration.pkl")
SHOW_PREVIEW = True             # 헤드리스(디스플레이 없는) 라파면 False
