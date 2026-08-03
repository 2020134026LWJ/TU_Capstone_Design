#!/usr/bin/env python3
"""주행 시간 측정기 — MQTT를 옆에서 듣기만 한다 (서버·AGV 무수정).

무엇을 재나
  ① 칸 이동  : forward 명령 발행 → 목표 노드 마커 도착      (한 칸 주행 순수 시간)
  ② 회전      : turn_* 명령 → cmd_ack                        (turn_left/right/180 별도 집계)
  ③ 리프트    : lift_* 명령 → cmd_ack                        (lift_up/down 별도 집계)
  ④ 구간      : 작업대/선반 사이 이동                        (예: W1 → 1-1, 1-1 → W1)

  ④의 '구간'은 **앵커(작업대·선반 노드) 사이 경과시간**이다. 중간의 회전·리프트·
  스테이징 대기가 모두 포함된 실사용 시간이라, 칸 이동(①)의 단순 합과는 다르다.
  대기(staging)에 걸린 구간은 튀므로 요약에서 중앙값도 같이 낸다.

쓰는 법
    python3 -m tools.measure_timing              # 로컬 브로커
    python3 -m tools.measure_timing --host UB-Region5.local
    python3 -m tools.measure_timing --csv run1   # run1_cells.csv / run1_segments.csv 저장
  Ctrl+C 로 종료하면 요약표가 나온다.

주의
  - 노드/선반 라벨은 server/data/shelf_config.json이 단일 진실 (코드에 숫자 박지 않음)
  - forward 완료 판정은 '목표 노드 마커 도착'이다. 마커를 놓치면 그 칸은 집계에서 빠진다
    (엉뚱한 값이 평균을 오염시키지 않게 의도적으로 버린다)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import sys
import time
import warnings
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHELF_CFG = os.path.join(_HERE, "..", "server", "data", "shelf_config.json")

TOPIC_CMD = "/agv/cmd"
TOPIC_MARKER = "/agv/marker"
TOPIC_CMD_ACK = "/agv/cmd_ack"
TOPIC_ARRIVED = "warehouse/shelf/arrived"


# ─── 라벨 ────────────────────────────────────────────────────────────────────

def load_labels() -> Tuple[Dict[int, str], set, set]:
    """shelf_config.json → {노드: 라벨}, 선반노드 집합, 작업대노드 집합"""
    with open(_SHELF_CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    labels: Dict[int, str] = {}
    shelves = set()
    stations = set()
    for node, v in cfg.get("shelves", {}).items():
        labels[int(node)] = v["label"]
        shelves.add(int(node))
    for node, v in cfg.get("workstations", {}).items():
        labels[int(node)] = v["label"]
        stations.add(int(node))
    for node, v in cfg.get("inbound_station", {}).items():
        labels[int(node)] = v["label"]
        stations.add(int(node))
    return labels, shelves, stations


def fmt_node(node: Optional[int], labels: Dict[int, str]) -> str:
    if node is None:
        return "?"
    lab = labels.get(node)
    return f"{lab}({node})" if lab else str(node)


# ─── 집계 ────────────────────────────────────────────────────────────────────

class Stats:
    """이름별 시간 샘플 모음"""

    def __init__(self) -> None:
        self.samples: Dict[str, List[float]] = defaultdict(list)

    def add(self, key: str, dt: float) -> None:
        self.samples[key].append(dt)

    def rows(self) -> List[tuple]:
        out = []
        for key, xs in sorted(self.samples.items()):
            out.append((key, len(xs), statistics.mean(xs), statistics.median(xs),
                        min(xs), max(xs)))
        return out

    def all_samples(self) -> List[float]:
        return [x for xs in self.samples.values() for x in xs]


def print_table(title: str, rows: List[tuple], unit: str = "초") -> None:
    if not rows:
        return
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))
    print(f"{'':22} {'n':>4} {'평균':>8} {'중앙':>8} {'최소':>8} {'최대':>8}")
    for key, n, mean, med, lo, hi in rows:
        print(f"{key:22} {n:>4} {mean:>8.2f} {med:>8.2f} {lo:>8.2f} {hi:>8.2f}")
    print(f"{'':22} {'':>4} {'(' + unit + ')':>8}")


# ─── 측정기 ──────────────────────────────────────────────────────────────────

class Meter:
    def __init__(self, labels, shelves, stations, verbose=True):
        self.labels = labels
        self.shelves = shelves
        self.stations = stations
        self.anchors = shelves | stations
        self.verbose = verbose

        # 로봇별 진행 중인 forward: rid → (target_node, t0)
        self._fwd: Dict[int, Tuple[int, float]] = {}
        # 로봇별 진행 중인 turn/lift: rid → (cmd, t0)
        self._act: Dict[int, Tuple[str, float]] = {}
        # 로봇별 마지막 앵커: rid → (node, t_arrival)
        self._anchor: Dict[int, Tuple[int, float]] = {}
        # 로봇별 현재 노드 (모든 마커로 갱신) — lift가 선반에서 났는지 작업대에서 났는지 판별용
        self._at: Dict[int, int] = {}
        # phase 시작: rid → (shelf_label, t0)
        self._deliver_start: Dict[int, Tuple[str, float]] = {}   # 선반 픽업 시점
        self._return_start: Dict[int, Tuple[str, float]] = {}    # 작업대 재픽업 시점

        self.cells = Stats()      # 칸 이동
        self.turns = Stats()      # 회전
        self.lifts = Stats()      # 리프트
        self.segments = Stats()   # 앵커 간 구간 (사람 대기 포함 — 참고용)
        self.deliver = Stats()    # 배달: 선반 픽업 → 작업대 도착 (사람 대기 없음)
        self.ret = Stats()        # 반납: 작업대 재픽업 → 선반 반납 (사람 대기 없음)

        self.cell_log: List[tuple] = []     # CSV용
        self.segment_log: List[tuple] = []
        self.phase_log: List[tuple] = []

    # -- 콜백 --------------------------------------------------------------

    def on_cmd(self, d: dict) -> None:
        rid, cmd = d.get("rid"), d.get("cmd")
        if rid is None or cmd is None:
            return
        now = time.time()
        if cmd == "forward":
            tgt = d.get("target_node")
            if tgt is not None:
                self._fwd[rid] = (int(tgt), now)
        elif cmd.startswith("turn") or cmd.startswith("lift"):
            self._act[rid] = (cmd, now)

        # ── phase 경계: lift 명령이 어디서 났는지로 배달/반납 시작·끝을 잡는다 ──
        # (사람 대기는 '작업대 도착 ~ shelf_complete' 사이라, 아래 경계엔 절대 안 낀다)
        if cmd in ("lift_up", "lift_down"):
            here = self._at.get(rid)
            shelf_id = d.get("shelf_id")
            label = self.labels.get(int(shelf_id)) if shelf_id is not None else None
            if cmd == "lift_up" and here in self.shelves:
                # 선반에서 들어올림 = 배달 시작
                self._deliver_start[rid] = (label or fmt_node(here, self.labels), now)
            elif cmd == "lift_up" and here in self.stations:
                # 작업대에서 다시 들어올림 = 반납 시작
                self._return_start[rid] = (label or "?", now)
            elif cmd == "lift_down" and here in self.shelves:
                # 선반에 내려놓음 = 반납 끝
                st = self._return_start.pop(rid, None)
                if st:
                    dt = now - st[1]
                    self.ret.add(f"반납 {st[0]}", dt)
                    self.phase_log.append((now, rid, "return", st[0], dt))
                    if self.verbose:
                        print(f"[반납] AGV-{rid} {st[0]:>6} 재픽업→반납   {dt:6.2f}초")

    def on_marker(self, d: dict) -> None:
        rid, node = d.get("rid"), d.get("marker_id")
        if rid is None or node is None:
            return
        node = int(node)
        now = time.time()
        self._at[rid] = node   # 현재 위치 갱신 (phase의 lift 위치 판별용)

        # ① 칸 이동 — 목표 노드에 도착했을 때만 인정
        pend = self._fwd.get(rid)
        if pend and pend[0] == node:
            dt = now - pend[1]
            key = f"AGV-{rid}"
            self.cells.add(key, dt)
            self.cell_log.append((now, rid, node, dt))
            del self._fwd[rid]
            if self.verbose:
                print(f"  [칸] AGV-{rid} → {fmt_node(node, self.labels):>10}  {dt:6.2f}초")

        # ④ 구간 — 앵커(작업대/선반)에 도착할 때마다 직전 앵커와의 경과 기록
        if node in self.anchors:
            prev = self._anchor.get(rid)
            if prev and prev[0] != node:
                dt = now - prev[1]
                key = f"{fmt_node(prev[0], self.labels)} → {fmt_node(node, self.labels)}"
                self.segments.add(key, dt)
                self.segment_log.append((now, rid, prev[0], node, dt))
                if self.verbose:
                    print(f"[구간] AGV-{rid} {key:26}  {dt:6.2f}초")
            self._anchor[rid] = (node, now)

    def on_arrived(self, d: dict) -> None:
        """warehouse/shelf/arrived → 배달 phase 끝 (선반이 작업대 도착).

        payload에 rid가 없어 선반 라벨(선반번호)로 매칭한다. 진행 중인 배달들 중
        같은 라벨을 찾아 닫는다 (단일 로봇이면 자명, 다중이어도 라벨로 구분됨).
        """
        label = d.get("선반번호")
        now = time.time()
        for rid, (lab, t0) in list(self._deliver_start.items()):
            if lab == label:
                dt = now - t0
                self.deliver.add(f"배달 {lab}", dt)
                self.phase_log.append((now, rid, "deliver", lab, dt))
                del self._deliver_start[rid]
                if self.verbose:
                    print(f"[배달] AGV-{rid} {lab:>6} 픽업→작업대도착 {dt:6.2f}초")
                return

    def on_ack(self, d: dict) -> None:
        rid, cmd = d.get("rid"), d.get("cmd")
        if rid is None or cmd is None:
            return
        pend = self._act.get(rid)
        if not pend or pend[0] != cmd:
            return
        dt = time.time() - pend[1]
        del self._act[rid]
        tgt = self.turns if cmd.startswith("turn") else self.lifts
        tgt.add(f"{cmd} (AGV-{rid})", dt)
        if self.verbose:
            kind = "회전" if cmd.startswith("turn") else "리프트"
            print(f"  [{kind}] AGV-{rid} {cmd:12}  {dt:6.2f}초")

    # -- 요약 --------------------------------------------------------------

    def summary(self) -> None:
        print("\n" + "=" * 66)
        print("측정 요약")
        print("=" * 66)

        print_table("① 칸 이동 (forward 발행 → 목표 노드 마커)", self.cells.rows())
        cs = self.cells.all_samples()
        if cs:
            print(f"\n  ★ 칸 이동 전체 평균: {statistics.mean(cs):.2f}초  "
                  f"(중앙 {statistics.median(cs):.2f} / n={len(cs)})")

        print_table("② 회전 (명령 → cmd_ack)", self.turns.rows())
        print_table("③ 리프트 (명령 → cmd_ack)", self.lifts.rows())

        # ★ 핵심: 사람 대기가 안 낀 두 phase (선반 픽업 기준)
        print_table("④ 배달 — 선반 픽업 → 작업대 도착 (사람 대기 없음)",
                    self.deliver.rows())
        ds = self.deliver.all_samples()
        if ds:
            print(f"\n  ★ 배달 전체 평균: {statistics.mean(ds):.2f}초  "
                  f"(중앙 {statistics.median(ds):.2f} / n={len(ds)})")

        print_table("⑤ 반납 — 작업대 재픽업 → 선반 반납 (사람 대기 없음)",
                    self.ret.rows())
        rs = self.ret.all_samples()
        if rs:
            print(f"\n  ★ 반납 전체 평균: {statistics.mean(rs):.2f}초  "
                  f"(중앙 {statistics.median(rs):.2f} / n={len(rs)})")

        # 참고용: 앵커 간 구간 (사람 대기 포함 → 못 믿음, 조용히 CSV에만)
        if not (cs or ds or rs):
            print("\n  (수집된 샘플이 없다 — 서버·AGV가 떠 있고 주문이 도는지 확인)")
        print()

    def write_csv(self, prefix: str) -> None:
        import csv
        with open(f"{prefix}_cells.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "rid", "arrive_node", "label", "seconds"])
            for ts, rid, node, dt in self.cell_log:
                w.writerow([f"{ts:.3f}", rid, node, self.labels.get(node, ""), f"{dt:.3f}"])
        with open(f"{prefix}_segments.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "rid", "from_node", "from_label",
                        "to_node", "to_label", "seconds"])
            for ts, rid, a, b, dt in self.segment_log:
                w.writerow([f"{ts:.3f}", rid, a, self.labels.get(a, ""),
                            b, self.labels.get(b, ""), f"{dt:.3f}"])
        with open(f"{prefix}_phases.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "rid", "phase", "shelf_label", "seconds"])
            for ts, rid, phase, lab, dt in self.phase_log:
                w.writerow([f"{ts:.3f}", rid, phase, lab, f"{dt:.3f}"])
        print(f"CSV 저장: {prefix}_cells.csv / {prefix}_segments.csv / {prefix}_phases.csv")


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="AGV 주행 시간 측정 (MQTT 수동 관찰)")
    p.add_argument("--host", default="localhost", help="MQTT 브로커 (기본 localhost)")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--csv", metavar="PREFIX", help="종료 시 CSV 저장 (PREFIX_cells.csv 등)")
    p.add_argument("-q", "--quiet", action="store_true", help="실시간 출력 없이 요약만")
    args = p.parse_args()

    labels, shelves, stations = load_labels()
    meter = Meter(labels, shelves, stations, verbose=not args.quiet)

    def on_connect(c, u, flags, rc):
        for t in (TOPIC_CMD, TOPIC_MARKER, TOPIC_CMD_ACK, TOPIC_ARRIVED):
            c.subscribe(t)
        print(f"[측정] {args.host}:{args.port} 연결 — 구독: cmd / marker / cmd_ack / arrived")
        print("[측정] Ctrl+C 로 종료하면 요약이 나옵니다\n")

    def on_message(c, u, msg):
        try:
            d = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if msg.topic == TOPIC_CMD:
            meter.on_cmd(d)
        elif msg.topic == TOPIC_MARKER:
            meter.on_marker(d)
        elif msg.topic == TOPIC_CMD_ACK:
            meter.on_ack(d)
        elif msg.topic == TOPIC_ARRIVED:
            meter.on_arrived(d)

    # paho 1.x / 2.x 양쪽 지원 (라파 1.6.1 / PC 2.x). v1 콜백 시그니처를 계속 쓰되,
    # 2.x가 그 자체로 내는 DeprecationWarning은 요약 출력을 더럽히므로 여기서만 끈다.
    cid = f"timing-{int(time.time())}"
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)  # 2.x
        except AttributeError:
            client = mqtt.Client(client_id=cid)                                    # 1.x
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, 60)
    except Exception as e:
        sys.exit(f"[측정] 브로커 연결 실패: {e}")

    def finish(*_):
        client.loop_stop()
        meter.summary()
        if args.csv:
            meter.write_csv(args.csv)
        sys.exit(0)

    signal.signal(signal.SIGINT, finish)
    client.loop_forever()


if __name__ == "__main__":
    main()
