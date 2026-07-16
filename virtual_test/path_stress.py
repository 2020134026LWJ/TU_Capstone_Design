#!/usr/bin/env python3
"""경로 알고리즘 랜덤 스트레스 테스트 (헤드리스, 독립 오라클 대조).

★ server.main 과 무관한 독립 프로그램. MQTT/서버 연결 없이 경로 계산(A*)만 직접 돌린다.

── 무엇을 검사하나 ────────────────────────────────────────────────────────────
 [단일-A] 빈 맵 최단경로 : 랜덤 start/goal/heading/turn_penalty → A*
          → A*비용 == 독립 Dijkstra 오라클 최적비용 (=진짜 최적경로로 갔나)
 [단일-B] 선반통과금지   : 위와 같되 선반 노드를 못 지나가게(excluded_transit)
          → 오라클도 같은 제약. 경로가 선반을 관통하지 않는지도 확인
 [단일-C] 제자리(start==goal) 엣지케이스 → 길이 1 경로여야
 [다중-R] 랜덤 2~4대   : 공유 예약에 순차 계획+커밋 → 충돌/스왑 없는지
 [다중-X] 적대적       : 일부러 정면충돌(서로 목적지 맞바꿈) / 병목 한 노드로 몰기
          → 균등랜덤이 안 만드는 '어려운' 충돌 상황을 강제로 던짐

 커버 안 하는 것(의도적): staging/포워딩/인터셉트 워크플로우(=pytest 100개 담당),
                          soft_avoid 통행권, corridor 영구점유, 다중해의 최적성(충돌회피만 봄)

── 로그 ──────────────────────────────────────────────────────────────────────
 매 N건마다 [hb] 요약 + [예시] 방금 검사한 구체적 시나리오 하나를 그대로 보여줌.
 FAIL 시에만 [FAIL] 상세. stdout flush → tail -f 실시간 관전.

실행:  python3 -m virtual_test.path_stress              # 무한
       python3 -m virtual_test.path_stress --max-iters 50000
Ctrl+C 로 종료.
"""
import os
import sys
import time
import random
import heapq
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.planning.path_planner import PathPlanner              # noqa: E402
from server.planning.reservation_service import ReservationService  # noqa: E402

MAP_FILE = os.path.join(PROJECT_ROOT, "server", "data", "map.json")
DIR_OF = {0: 0, 90: 1, 180: 2, 270: 3}
HEADINGS = [None, 0, 90, 180, 270]
TPS = [0.0, 0.3, 0.5, 1.0]


# ─── 독립 오라클 (같은 비용모델 Dijkstra). excluded=통과금지 노드 ───
def dijkstra_oracle(planner, start, goal, start_dir, tp, excluded=None):
    excluded = excluded or set()
    pq = [(0.0, start, start_dir)]
    best = {(start, start_dir): 0.0}
    while pq:
        g, node, d = heapq.heappop(pq)
        if node == goal:
            return g
        if g > best.get((node, d), float("inf")):
            continue
        for nxt, cost in planner.graph.get(node, []):
            if nxt in excluded and nxt != goal and nxt != start:
                continue
            nd = planner._node_direction(node, nxt)
            extra = tp if (d != -1 and nd != d) else 0.0
            ng = g + cost + extra
            if ng < best.get((nxt, nd), float("inf")):
                best[(nxt, nd)] = ng
                heapq.heappush(pq, (ng, nxt, nd))
    return None


def path_cost(planner, node_path, start_dir, tp):
    g, d = 0.0, start_dir
    for a, b in zip(node_path, node_path[1:]):
        if a == b:
            g += 1.0
            continue
        cost = next((c for nxt, c in planner.graph.get(a, []) if nxt == b), None)
        if cost is None:
            return None
        nd = planner._node_direction(a, b)
        g += cost + (tp if (d != -1 and nd != d) else 0.0)
        d = nd
    return g


# ─── 단일 로봇 ───
def check_single(planner, nodes, shelf_nodes):
    mode = random.random()
    start = random.choice(nodes)
    if mode < 0.06:                                   # [C] 제자리
        goal = start
    else:
        goal = random.choice([n for n in nodes if n != start])
    heading = random.choice(HEADINGS)
    tp = random.choice(TPS)
    use_excl = mode >= 0.55 and start != goal          # [B] 선반통과금지 (약 45%)
    excluded = set(shelf_nodes) if use_excl else None
    start_dir = DIR_OF.get(heading, -1) if heading is not None else -1
    kind = "제자리" if start == goal else ("선반금지" if use_excl else "빈맵")

    timed = planner.astar_with_time(
        start, goal, start_heading=heading, turn_penalty=tp,
        excluded_transit=excluded, max_time=60)
    tag = f"[단일:{kind}] {start}→{goal} h={heading} tp={tp}"

    if timed is None:
        return f"{tag} :: 경로 없음(None)", tag
    node_path = [n for n, _ in timed]
    if node_path[0] != start:
        return f"{tag} :: 시작 불일치 {node_path[0]}", tag
    if node_path[-1] != goal:
        return f"{tag} :: 도착 불일치 {node_path[-1]}", tag
    if start == goal and len(node_path) != 1:
        return f"{tag} :: 제자리인데 경로길이 {len(node_path)}", tag
    if excluded:
        interior = node_path[1:-1]
        bad = [n for n in interior if n in excluded]
        if bad:
            return f"{tag} :: 선반 관통 {bad}", tag
    ac = path_cost(planner, node_path, start_dir, tp)
    if ac is None:
        return f"{tag} :: 순간이동(인접X) {node_path}", tag
    oc = dijkstra_oracle(planner, start, goal, start_dir, tp, excluded)
    if oc is None:
        return f"{tag} :: 오라클 도달실패", tag
    if ac > oc + 1e-6:
        return f"{tag} :: ⚠최적아님 A*={ac:.2f} > 최적={oc:.2f}  경로={node_path}", tag
    if ac < oc - 1e-6:
        return f"{tag} :: 오라클보다 낮음 {ac:.2f}<{oc:.2f}(하네스버그)", tag
    desc = f"{tag} → {len(node_path)}칸 {node_path} 비용{ac:.2f} =최적 ✅"
    return None, desc


# ─── 다중 로봇 ───
def _plan_commit(planner, res, rid, start, goal, tp):
    heading = random.choice(HEADINGS)
    timed = planner.astar_with_time(
        start, goal, reservation=res, rid=rid,
        start_heading=heading, turn_penalty=tp, max_time=60)
    if timed is None:
        return None, "None"
    node_path = [n for n, _ in timed]
    if node_path[0] != start or node_path[-1] != goal:
        return "ENDPOINT", node_path
    if not res.commit(rid, node_path, dwell=1):
        return None, "commit실패"
    return node_path, node_path


def check_multi(planner, nodes, adversarial):
    tp = random.choice(TPS)
    res = ReservationService()
    if adversarial and random.random() < 0.5:
        # [X] 정면충돌: A↔B 서로 목적지 맞바꿈 + 가끔 3번째 훼방꾼
        a, b = random.sample(nodes, 2)
        specs = [(1, a, b), (2, b, a)]
        kind = "적대적:정면"
        if random.random() < 0.5:
            c = random.choice([n for n in nodes if n not in (a, b)])
            specs.append((3, c, random.choice([a, b])))
    elif adversarial:
        # [X] 병목: 여러 대가 한 노드(bottleneck)를 지나 반대편으로
        k = random.randint(2, 4)
        bott = random.choice(nodes)
        nbr = [n for n, _ in planner.graph.get(bott, [])]   # neighbors() 없는 옛 버전도 호환
        specs = []
        for i in range(k):
            s = random.choice(nodes)
            specs.append((i + 1, s, bott if i == 0 else random.choice(nbr or nodes)))
        kind = f"적대적:병목@{bott}"
    else:
        # [R] 랜덤 2~4대
        k = random.randint(2, 4)
        starts = random.sample(nodes, k)
        specs = [(i + 1, starts[i],
                  random.choice([n for n in nodes if n != starts[i]])) for i in range(k)]
        kind = "랜덤"

    committed = []
    for rid, s, g in specs:
        path, _ = _plan_commit(planner, res, rid, s, g, tp)
        if path == "ENDPOINT":
            return f"[다중:{kind}] 로봇{rid} 끝점 불일치", f"[다중:{kind}]"
        if path:
            committed.append((rid, path))

    # 독립 충돌 검사 (dwell=1 셀 + 스왑)
    occ, edges = {}, {}
    for rid, path in committed:
        for i, node in enumerate(path):
            for dt in (0, 1):
                key = (node, i + dt)
                if occ.get(key, rid) != rid:
                    return (f"[다중:{kind}] 💥충돌 노드{node}@t={i+dt} "
                            f"로봇{occ[key]}·{rid} tp={tp}"), f"[다중:{kind}]"
                occ[key] = rid
        for i in range(len(path) - 1):
            a, b, t = path[i], path[i + 1], i
            if edges.get((b, a, t), rid) != rid:
                return (f"[다중:{kind}] 💥스왑 {a}↔{b}@t={t} tp={tp}"), f"[다중:{kind}]"
            edges[(a, b, t)] = rid

    blocked = len(specs) - len(committed)
    desc = (f"[다중:{kind}] {len(specs)}대 → 커밋 {len(committed)}/blocked {blocked}, "
            f"충돌 0 ✅  " + " ".join(f"R{r}:{p[0]}→{p[-1]}({len(p)}칸)"
                                     for r, p in committed[:3]))
    return None, (desc, len(committed), blocked)


def run(log_every, fail_log, max_iters=0, multi_ratio=0.4, adversarial_ratio=0.5):
    planner = PathPlanner(MAP_FILE)
    nodes = list(planner.nodes.keys())
    shelf_nodes = planner.shelf_nodes

    n = fails = n_single = n_multi = 0
    m_committed = m_blocked = 0
    last_single = last_multi = "(아직 없음)"
    t0 = time.time()

    print("=" * 92, flush=True)
    print("  경로 알고리즘 스트레스 테스트 — server.main 무관, 독립 실행", flush=True)
    print(f"  맵: {len(nodes)}노드 (선반 {len(shelf_nodes)}개) | 시나리오: 단일(빈맵/선반금지/제자리)"
          f" + 다중(랜덤/적대적)", flush=True)
    print(f"  다중 비율 {multi_ratio:.0%}, 그 중 적대적 {adversarial_ratio:.0%} | "
          f"검증: A*==Dijkstra오라클 / 커밋경로 충돌0", flush=True)
    print("=" * 92, flush=True)

    def _final_report(reason):
        el = time.time() - t0
        status = "✅ 전부 통과 (FAIL 0건)" if fails == 0 else f"❌ FAIL {fails}건 — path_stress_fails.log 확인"
        print("\n" + "=" * 92, flush=True)
        print(f"  [{reason}] 최종 집계", flush=True)
        print(f"  총 {n:,}건  (단일 {n_single:,} · 다중 {n_multi:,}) | {status}", flush=True)
        if n_multi:
            rate = 100 * m_committed / max(1, m_committed + m_blocked)
            print(f"  다중로봇: 충돌없이 커밋 {m_committed:,}대 / blocked {m_blocked:,}대 "
                  f"(커밋률 {rate:.1f}%)", flush=True)
        print(f"  경과 {el:.1f}s | 평균 {n/max(el,1e-9):,.0f} it/s", flush=True)
        print("=" * 92, flush=True)

    try:
      while True:
        n += 1
        if random.random() < multi_ratio:
            n_multi += 1
            problem, out = check_multi(planner, nodes, random.random() < adversarial_ratio)
            if problem is None:
                last_multi = out[0]
                m_committed += out[1]
                m_blocked += out[2]
        else:
            n_single += 1
            problem, out = check_single(planner, nodes, shelf_nodes)
            if problem is None:
                last_single = out

        if problem:
            fails += 1
            line = f"[FAIL #{fails}] iter={n} {problem}"
            print(line, flush=True)
            with open(fail_log, "a") as f:
                f.write(line + "\n")

        if n % log_every == 0:
            el = time.time() - t0
            status = "✅ 무결점" if fails == 0 else f"❌ FAIL {fails}건"
            print(f"\n[hb] 총 {n:,}건 | 단일 {n_single:,} · 다중 {n_multi:,} | {status} | "
                  f"{n/el:,.0f} it/s | {el:.0f}s 경과", flush=True)
            if n_multi:
                rate = 100 * m_committed / max(1, m_committed + m_blocked)
                print(f"     다중로봇 누적: 충돌없이 커밋 {m_committed:,}대 / "
                      f"blocked {m_blocked:,}대 (커밋률 {rate:.1f}%)", flush=True)
            print(f"     [예시-단일] {last_single}", flush=True)
            print(f"     [예시-다중] {last_multi}", flush=True)

        if max_iters and n >= max_iters:
            _final_report("완료")
            return fails
    except KeyboardInterrupt:
        _final_report("Ctrl+C 중단")
        return fails


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--log-every", type=int, default=10000)
    p.add_argument("--max-iters", type=int, default=0, help="0=무한")
    p.add_argument("--multi-ratio", type=float, default=0.4)
    p.add_argument("--adversarial-ratio", type=float, default=0.5,
                   help="다중 중 적대적(정면충돌/병목) 비율")
    p.add_argument("--fail-log", default=os.path.join(PROJECT_ROOT, "path_stress_fails.log"))
    a = p.parse_args()
    run(a.log_every, a.fail_log, a.max_iters, a.multi_ratio, a.adversarial_ratio)
