#!/usr/bin/env bash
# Isaac Sim 디지털 트윈 실행 (TWIN=1) — 실물/벤치가 발행한 마커를 따라간다.
#
#   ./isaac_simulation/run_twin.sh            # 칸 이동 3.31초 / 회전 2.04초 (실측 최소값 기본)
#   ./isaac_simulation/run_twin.sh 4.0 2.3    # 칸 이동 4.0초 / 회전 2.3초 (수동 지정)
#   ※ EMA는 꺼져 있어(고정 모드) 이 값이 매 스텝 그대로 쓰인다. 리프트는 TWIN_LIFT_SECS(기본 2.13).
#   ※ 기본값 출처 = HIL 실측(2026-07-21, 최소값): 이동 3.31 / 회전 2.04 / 리프트 2.13초.
#
# [주의] TWIN=1 이 없으면 Isaac이 '트윈'이 아니라 'AGV 본인'이 되어 마커를 직접 발행한다.
#        실물/벤치와 동시에 띄우면 발행자가 둘 → 서버가 도착을 두 번 받아 상태가 꼬인다.
#
# 일반 시뮬(Isaac이 AGV 역할)은 이 스크립트 말고 아래를 직접 실행:
#   ~/isaacsim/_build/linux-x86_64/release/python.sh isaac_simulation/step7_kinematic.py

set -e
cd "$(dirname "$0")/.."          # 어디서 실행하든 TU_Capstone_Design 기준

export TWIN=1
export TWIN_EDGE_SECS="${1:-3.31}"  # 직진(칸 이동) 고정 시간 (실측 최소 3.31초)
export TWIN_TURN_SECS="${2:-2.04}"  # 90° 회전 고정 시간 (실측 최소 2.04초)
export TWIN_LIFT_SECS="${3:-2.13}"  # 리프트 고정 시간 (실측 최소 2.13초)
export PYTHONUNBUFFERED=1           # 로그를 파일로 넘길 때 print가 묻히지 않게

echo "[트윈] TWIN=1  이동=${TWIN_EDGE_SECS}s  회전=${TWIN_TURN_SECS}s  리프트=${TWIN_LIFT_SECS}s (실측 최소값)"
exec ~/isaacsim/_build/linux-x86_64/release/python.sh isaac_simulation/step7_kinematic.py
