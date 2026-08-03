"""route_sheet.py — jw_headless에 사람이 칠 숫자를 알려주는 '타이핑 시트'
TU Capstone Design - AGV 물류 피킹 시스템

**왜 (2026-07-25)**
  rpi_seq(우리 브릿지)로 돌리면 2~3번째 명령에서 로봇이 튀는데, jw_headless에
  사람이 직접 치면 안 튄다. 코드 대조로는 차이를 못 찾았다.
  → **jw_headless를 하나도 안 고치고, 사람이 치는 조건 그대로 두되,
     "무엇을 칠지"만 컴퓨터가 알려준다.**

  지금까지 사람 테스트는 명령 1~5개짜리 단발이었다. 서버가 내는 것과 **같은 경로,
  같은 길이**를 사람이 쳐서 돌려보면, "브릿지냐 / 명령 개수냐"가 갈린다.
    · 사람이 쳐도 2~3번째에서 튄다 → 브릿지 무죄. STM/물리 확정
    · 끝까지 깨끗하다              → 브릿지에 아직 못 찾은 차이가 있다

  경로와 명령 변환은 **서버와 똑같은 로직**을 쓴다(`_path_to_commands` 복제).
  그래야 "서버가 냈을 그 시퀀스"를 사람이 재현하는 게 된다.

**실행 (노트북에서 — 라파 SSH 창 옆에 띄워두고 보면서 친다)**
    python3 tools/route_sheet.py --from 8 --to 19
    python3 tools/route_sheet.py --from 8 --to 19 --heading 90 --step
    python3 tools/route_sheet.py --preset lap1        # 왕복 한 바퀴
    python3 tools/route_sheet.py --list               # 노드 번호 확인

  --step : 한 줄씩 크게 보여주고 Enter 로 넘긴다 (치면서 보기 편함)
  --carry: 선반을 든 상태 — 선반 노드 통과 금지 (서버와 동일 규칙)
"""

import argparse
import json
import os
import sys
from collections import deque

CMD_CODE = {
    "forward": 1, "stop": 2, "lift_up": 3, "lift_down": 4,
    "turn_left": 5, "turn_right": 6, "turn_180": 7,
}
HEADING_NAME = {0: "북", 90: "동", 180: "남", 270: "서"}

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "..", "server", "data")

# 미리 짜둔 시나리오 (서버가 실제로 내는 흐름과 같은 모양)
PRESETS = {
    # 홈에서 선반까지 갔다가 그대로 되돌아오기 — 드리프트 누적이 제일 잘 보인다
    "lap1": [("8", "19"), ("19", "8")],
    "lap2": [("32", "27"), ("27", "32")],
    # 픽업→작업대→반납 (실제 주문 한 건과 같은 길이)
    "order1": [("8", "19"), ("19", "8"), ("8", "19")],
}


def load_map():
    with open(os.path.join(_DATA, "map.json"), encoding="utf-8") as f:
        m = json.load(f)
    nodes = {n["id"]: (n["x"], n["y"]) for n in m["nodes"]}
    types = {n["id"]: n.get("type", "M") for n in m["nodes"]}
    return nodes, types


def neighbors(nid, nodes):
    """격자 4방향 이웃 (좌표가 1칸 차이나는 노드)."""
    x, y = nodes[nid]
    out = []
    for other, (ox, oy) in nodes.items():
        if other == nid:
            continue
        if abs(ox - x) + abs(oy - y) == 1.0:
            out.append(other)
    return sorted(out)


def bfs(start, goal, nodes, blocked):
    """최단 경로 (단일 로봇 수동 주행이므로 시간축 A*가 아니라 BFS면 충분)."""
    if start == goal:
        return [start]
    q, seen = deque([[start]]), {start}
    while q:
        path = q.popleft()
        for nb in neighbors(path[-1], nodes):
            if nb in seen:
                continue
            if nb in blocked and nb != goal:
                continue
            if nb == goal:
                return path + [nb]
            seen.add(nb)
            q.append(path + [nb])
    return None


def node_direction(a, b, nodes):
    ax, ay = nodes[a]
    bx, by = nodes[b]
    dx, dy = bx - ax, by - ay
    if abs(dx) < abs(dy):
        return 0 if dy > 0 else 180
    return 90 if dx > 0 else 270


def path_to_commands(path, start_heading, nodes):
    """서버 `_movement_mixin._path_to_commands` 와 동일 규칙."""
    def turns(cur, tgt):
        d = (tgt - cur) % 360
        return {0: [], 90: ["turn_right"], 180: ["turn_180"], 270: ["turn_left"]}[d]

    cmds, heading = [], start_heading
    for i in range(1, len(path)):
        tgt = node_direction(path[i - 1], path[i], nodes)
        for t in turns(heading, tgt):
            cmds.append((t, None))
            heading = (heading + {"turn_right": 90, "turn_180": 180, "turn_left": 270}[t]) % 360
        cmds.append(("forward", path[i]))
        heading = tgt
    return cmds, heading


def main():
    p = argparse.ArgumentParser(description="jw_headless 타이핑 시트")
    p.add_argument("--from", dest="src", type=int, help="출발 노드")
    p.add_argument("--to", dest="dst", type=int, help="도착 노드")
    p.add_argument("--heading", type=int, default=90,
                   help="출발 heading (0=북 90=동 180=남 270=서). 홈 배치가 동향이면 90")
    p.add_argument("--carry", action="store_true", help="선반 든 상태 (선반 노드 통과 금지)")
    p.add_argument("--preset", choices=sorted(PRESETS), help=f"미리 짜둔 시나리오: {sorted(PRESETS)}")
    p.add_argument("--step", action="store_true", help="한 줄씩 보여주고 Enter 로 넘기기")
    p.add_argument("--list", action="store_true", help="노드 번호/좌표 표 출력")
    args = p.parse_args()

    nodes, types = load_map()

    if args.list:
        shelves = [n for n, t in types.items() if t == "S"]
        ws = [n for n, t in types.items() if t == "W"]
        print(f"노드 0~{max(nodes)} / 선반 {shelves} / 작업대 {ws}")
        for n in sorted(nodes):
            x, y = nodes[n]
            print(f"  {n:2d}  ({x:>4}, {y:>4})  {types[n]}")
        return 0

    legs = PRESETS[args.preset] if args.preset else None
    if legs is None:
        if args.src is None or args.dst is None:
            p.error("--from / --to 를 주거나 --preset 을 쓰세요 (--list 로 노드 확인)")
        legs = [(str(args.src), str(args.dst))]

    blocked = {n for n, t in types.items() if t == "S"} if args.carry else set()

    heading = args.heading
    sheet = []
    for src, dst in legs:
        src, dst = int(src), int(dst)
        path = bfs(src, dst, nodes, blocked)
        if not path:
            print(f"경로 없음: {src} → {dst}", file=sys.stderr)
            return 1
        cmds, heading = path_to_commands(path, heading, nodes)
        sheet.append((src, dst, path, cmds))

    total = sum(len(c) for _, _, _, c in sheet)
    print("=" * 58)
    print(f"  jw_headless 타이핑 시트 — 총 {total}개 명령")
    print(f"  시작 heading {args.heading}° ({HEADING_NAME.get(args.heading, '?')})"
          + ("  / 선반 든 상태" if args.carry else ""))
    print("=" * 58)

    seq = []
    for src, dst, path, cmds in sheet:
        print(f"\n[{src} → {dst}]  경로: {' → '.join(map(str, path))}")
        for name, target in cmds:
            seq.append((name, target))
            n = len(seq)
            tgt = f"→ 노드 {target}" if target is not None else ""
            print(f"   {n:2d})  치기: {CMD_CODE[name]}   {name:<11} {tgt}")

    print("\n" + "=" * 58)
    print("  숫자만 나열:  " + " ".join(str(CMD_CODE[n]) for n, _ in seq))
    print("=" * 58)
    print("  ⚠ jw_headless 켠 다음에 STM 리셋 (부팅 DONE으로 첫 명령이 열림)")
    print("  ⚠ 서버(server.main)·rpi_seq 는 끄고 — UART/카메라는 한 프로세스만")
    print("  ⚠ EVT_DONE 프롬프트가 뜬 뒤에 다음 숫자를 칠 것")

    if args.step:
        print("\n--- 한 줄씩 모드: Enter 로 다음 ---")
        for i, (name, target) in enumerate(seq, 1):
            input(f"\n  [{i}/{len(seq)}]  ►►►  {CMD_CODE[name]}  ◄◄◄   ({name}"
                  + (f", 노드 {target}" if target is not None else "") + ")   [Enter]")
        print("\n  시퀀스 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
