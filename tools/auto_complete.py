#!/usr/bin/env python3
"""선반 도착 → shelf_complete 자동 발행 (측정용 사람 대리).

왜 필요한가
  구간 측정(measure_timing ④)이 사람이 손으로 치는 shelf_complete 대기에 오염된다.
  이 스크립트가 warehouse/shelf/arrived를 듣고 **일정한 지연 뒤** shelf/complete를
  자동 발행하면, 사람 속도가 빠지고 구간 시간이 재현 가능해진다.

  --delay 0    : 도착 즉시 완료 → 순수 로봇 사이클 시간 (기본)
  --delay 3.0  : 3초 고정 피킹 시간 시뮬 (매번 똑같이)

쓰는 법
    python3 -m tools.auto_complete                 # 즉시 완료
    python3 -m tools.auto_complete --delay 3       # 3초 고정
    python3 -m tools.auto_complete --host UB-Region5.local

  measure_timing 과 같이 띄워두고 주문을 넣으면 사람 개입 없이 완주 → ④가 깨끗해진다.

주의
  - GUI(warehouse_gui)와 동시에 켜지 말 것. shelf_complete가 두 번 나가 꼬인다.
  - 측정 전용. 실제 시연에선 사람이 GUI로 눌러야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import warnings

import paho.mqtt.client as mqtt

TOPIC_ARRIVED = "warehouse/shelf/arrived"
TOPIC_COMPLETE = "warehouse/shelf/complete"


def main() -> None:
    p = argparse.ArgumentParser(description="선반 도착 시 shelf_complete 자동 발행 (측정용)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--delay", type=float, default=0.0,
                   help="도착 후 완료까지 지연(초). 0=즉시(기본), 예: 3=고정 피킹시간 시뮬")
    args = p.parse_args()

    def on_connect(c, u, flags, rc):
        c.subscribe(TOPIC_ARRIVED)
        print(f"[auto] {args.host}:{args.port} 연결 — '{TOPIC_ARRIVED}' 구독")
        print(f"[auto] 도착 시 {args.delay}초 뒤 shelf_complete 자동 발행")
        print(f"[auto] ⚠ GUI와 동시에 켜지 말 것 (완료 이중발행)\n")

    def on_message(c, u, msg):
        try:
            d = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        ws = d.get("작업대")
        uid = d.get("사용자ID")
        shelf = d.get("선반번호")
        if ws is None:
            return

        def fire():
            if args.delay > 0:
                time.sleep(args.delay)
            payload = {"사용자ID": uid, "작업대": ws}
            c.publish(TOPIC_COMPLETE, json.dumps(payload, ensure_ascii=False))
            print(f"[auto] 완료 발행 → 작업대 {ws}, 선반 {shelf} "
                  f"(도착 {args.delay}초 뒤)")

        # 지연 발행을 별도 스레드로 (여러 작업대 동시 도착에도 안 막힘)
        threading.Thread(target=fire, daemon=True).start()

    cid = f"autocomplete-{int(time.time())}"
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)  # paho 2.x
        except AttributeError:
            client = mqtt.Client(client_id=cid)                                    # paho 1.x
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, 60)
    except Exception as e:
        sys.exit(f"[auto] 브로커 연결 실패: {e}")

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[auto] 종료")


if __name__ == "__main__":
    main()
