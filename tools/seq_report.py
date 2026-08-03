"""seq_report.py — rpi_seq 진단 로그(JSONL)를 읽어 '누구 탓인가'로 정리
TU Capstone Design - AGV 물류 피킹 시스템

  rpi_seq.py가 남기는 ~/rpi_seq_{rid}_MMDD_HHMM.jsonl 을 명령 단위로 묶어서,
  각 명령이 어디까지 갔는지(서버 수신 → 발행 → STM 전송 → ACK → DONE → 실측)를
  한 줄로 만들고, 끊긴 지점으로 책임을 가른다.

판별 규칙 (로그에 있는 사실만으로)
  수신은 됐는데 발행이 없다      → 브릿지 (큐에 갇힘 / 앞 명령이 안 끝남)
  발행은 했는데 TX 패킷이 0      → 브릿지 (마커가 안 보여 보류 — 원본 동작)
  TX는 나갔는데 ACK가 없다       → 펌웨어·배선 (STM이 프레임을 못 받았거나 버림)
  ACK는 왔는데 DONE이 없다       → 펌웨어 (목표에 도달 못 함 / 물리적 걸림)
  다 왔는데 회전 실측이 틀리다   → 펌웨어 (IMU·재동기)
  다 왔는데 도착 노드가 다르다   → 펌웨어/물리 (또는 앞선 회전 오차의 누적)
  전부 정상인데 순서가 이상하다  → 서버 (cmd_recv 원문을 직접 볼 것)

실행
    python3 tools/seq_report.py ~/rpi_seq_1_0726_0210.jsonl
    python3 tools/seq_report.py *.jsonl --csv out.csv
"""

import argparse
import json
import sys
from collections import defaultdict


def load(paths):
    evs = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    evs.sort(key=lambda e: e.get("t", 0.0))
    return evs


def group(evs):
    """이벤트를 명령 단위로 묶는다. dispatch가 명령의 시작, done이 끝."""
    cmds, cur = [], None
    pending_recv = []
    for e in evs:
        ev = e.get("ev")
        if ev == "cmd_recv":
            pending_recv.append(e)
        elif ev == "dispatch":
            cur = {"cmd": e.get("cmd"), "target": e.get("target"),
                   "t_dispatch": e["t"], "held": e.get("held"),
                   "after_done": e.get("after_done"), "yaw0": e.get("yaw0"),
                   "tx": 0, "ack": None, "done": None, "holds": 0,
                   "turn_actual": None, "turn_err": None, "turn_expect": None,
                   "arrived_ok": None, "stall": None, "multi": 0}
            cmds.append(cur)
            if pending_recv:
                pending_recv.pop(0)
        elif cur is None:
            continue
        elif ev == "tx":
            cur["tx"] = max(cur["tx"], e.get("n", 0))
        elif ev == "hold_no_marker":
            cur["holds"] += 1
        elif ev == "multi_marker":
            cur["multi"] += 1
        elif ev == "stall":
            cur["stall"] = e.get("elapsed")
        elif ev == "ack":
            cur["ack"] = e.get("dt_dispatch")
        elif ev == "done":
            cur["done"] = e.get("dt_dispatch")
            cur["dt_ack"] = e.get("dt_ack")
            cur["turn_actual"] = e.get("turn_actual")
            cur["turn_err"] = e.get("turn_err")
            cur["turn_expect"] = e.get("turn_expect")
            cur["arrived_ok"] = e.get("arrived_ok")
            cur["mid"] = e.get("mid")
            cur = None
    # 발행조차 못 된 서버 명령
    orphans = pending_recv
    return cmds, orphans


def verdict(c):
    """이 명령이 어디서 끊겼나 → (책임, 설명)"""
    if c["done"] is None and c["ack"] is None and c["tx"] == 0:
        return "브릿지", f"발행했으나 STM에 한 패킷도 안 나감 (마커 보류 {c['holds']}회)"
    if c["done"] is None and c["ack"] is None:
        return "펌웨어", f"TX {c['tx']}패킷 나갔는데 ACK 없음 — STM이 프레임을 못 받았거나 버림"
    if c["done"] is None:
        return "펌웨어", "ACK는 왔는데 DONE 없음 — 목표 미달/물리적 걸림"
    if c["turn_err"] is not None and abs(c["turn_err"]) > 10.0:
        return "펌웨어", f"회전 실측 {c['turn_actual']:+.1f}° (지시 {c['turn_expect']:+.0f}°)"
    if c["arrived_ok"] is False:
        return "펌웨어", f"서버 target={c['target']} 인데 도착 mk={c.get('mid')}"
    return "정상", ""


def main():
    p = argparse.ArgumentParser(description="rpi_seq JSONL → 명령별 판별 리포트")
    p.add_argument("paths", nargs="+", help="rpi_seq_*.jsonl")
    p.add_argument("--csv", default=None, help="명령별 표를 CSV로도 저장")
    p.add_argument("--turn-limit", type=float, default=10.0,
                   help="회전 오차 경고 임계(도). 기본 10")
    args = p.parse_args()

    evs = load(args.paths)
    if not evs:
        print("이벤트가 없습니다.", file=sys.stderr)
        return 1
    cmds, orphans = group(evs)

    print(f"이벤트 {len(evs)}개 / 명령 {len(cmds)}개\n")
    hdr = (f"{'#':>3} {'명령':<10} {'tgt':>4} {'붙듦':>6} {'TX':>3} "
           f"{'ACK':>6} {'DONE':>6} {'실측':>8} {'오차':>7}  판별")
    print(hdr)
    print("-" * len(hdr))

    counts = defaultdict(int)
    turn_errs = []
    for i, c in enumerate(cmds, 1):
        who, why = verdict(c)
        counts[who] += 1
        ta = f"{c['turn_actual']:+.1f}" if c["turn_actual"] is not None else "-"
        te = f"{c['turn_err']:+.1f}" if c["turn_err"] is not None else "-"
        if c["turn_err"] is not None:
            turn_errs.append((i, c["cmd"], c["turn_err"]))
        # 노드 0 은 falsy 다. `target or '-'` 로 쓰면 0 번 노드가 '-' 로 찍힌다 (실제로 밟았음)
        tgt = "-" if c["target"] is None else str(c["target"])
        held = 0.0 if c["held"] is None else c["held"]
        print(f"{i:>3} {c['cmd']:<10} {tgt:>4} "
              f"{held:>6.2f} {c['tx']:>3} "
              f"{(c['ack'] if c['ack'] is not None else float('nan')):>6.2f} "
              f"{(c['done'] if c['done'] is not None else float('nan')):>6.2f} "
              f"{ta:>8} {te:>7}  "
              + ("" if who == "정상" else f"[{who}] {why}"))

    print("\n=== 요약 ===")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<6} {v}건")
    if orphans:
        print(f"  [서버/브릿지] 발행되지 못한 명령 {len(orphans)}건 "
              f"(마지막: {orphans[-1].get('raw')})")

    # ★ 회전 오차의 '모양'이 재동기 여부를 말해준다 (STM 로그 없이 판정하는 유일한 방법)
    if len(turn_errs) >= 3:
        vals = [e for _, _, e in turn_errs]
        print(f"\n=== 회전 오차 추이 ({len(vals)}회) ===")
        print("  " + "  ".join(f"{v:+.1f}" for v in vals))
        first, last = abs(vals[0]), abs(vals[-1])
        mono = all(abs(vals[i]) >= abs(vals[i - 1]) - 1.0 for i in range(1, len(vals)))
        big = max(abs(v) for v in vals)
        if mono and last > first + 5.0:
            print("  -> 단조 증가. 재동기가 안 도는 것으로 보인다 "
                  "(main.c 재동기 문턱 0.5° 그대로일 가능성). STM 리셋 주기를 짧게.")
        elif big > args.turn_limit:
            print(f"  -> 평소 작다가 튐(최대 {big:+.1f}°). 5° 넘어 dyaw 보정이 "
                  "급개입하는 모양 — 07-25 −78.4°와 같은 패턴.")
        else:
            print("  -> 작고 부호가 섞임. 회전 자체는 건전하다.")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["idx", "cmd", "target", "held", "tx", "ack", "done",
                        "turn_actual", "turn_expect", "turn_err", "arrived_ok",
                        "verdict"])
            for i, c in enumerate(cmds, 1):
                who, _ = verdict(c)
                w.writerow([i, c["cmd"], c["target"], c["held"], c["tx"], c["ack"],
                            c["done"], c["turn_actual"], c["turn_expect"],
                            c["turn_err"], c["arrived_ok"], who])
        print(f"\nCSV 저장: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
