#!/usr/bin/env bash
# Isaac Sim 디지털 트윈 실행 (TWIN=1) — 실물/벤치가 발행한 마커를 따라간다.
#
#   ./isaac_simulation/run_twin.sh            # 1칸 추정 3.0초로 시작
#   ./isaac_simulation/run_twin.sh 5.0        # 1칸 추정 5.0초 (실물이 느릴 때)
#
# [주의] TWIN=1 이 없으면 Isaac이 '트윈'이 아니라 'AGV 본인'이 되어 마커를 직접 발행한다.
#        실물/벤치와 동시에 띄우면 발행자가 둘 → 서버가 도착을 두 번 받아 상태가 꼬인다.
#
# 일반 시뮬(Isaac이 AGV 역할)은 이 스크립트 말고 아래를 직접 실행:
#   ~/isaacsim/_build/linux-x86_64/release/python.sh isaac_simulation/step7_kinematic.py

set -e
cd "$(dirname "$0")/.."          # 어디서 실행하든 TU_Capstone_Design 기준

export TWIN=1
export TWIN_EDGE_SECS="${1:-3.0}"   # 첫 칸 추정 (실측 1회면 바로 대체됨)
export PYTHONUNBUFFERED=1           # 로그를 파일로 넘길 때 print가 묻히지 않게

echo "[트윈] TWIN=1  TWIN_EDGE_SECS=${TWIN_EDGE_SECS}초 (첫 칸 추정)"
exec ~/isaacsim/_build/linux-x86_64/release/python.sh isaac_simulation/step7_kinematic.py
