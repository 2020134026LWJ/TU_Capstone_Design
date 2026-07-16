#!/usr/bin/env python3
"""교착 해소 퍼저 — 일반 교착(wait-for 사이클, 수정 54) 감지/해소 회귀 검사.

★ path_stress.py(planning), lifecycle_fuzz.py(예약청소) 와 또 다른 층: 교착 backstop.
  실제 RequestHandler를 세워 '마주 선 두 로봇' 교착을 랜덤 대량 생성하고,
  시스템이 (1) 감지하고 (2) *실제로* 풀어내는지 본다. server.main/MQTT 없이 in-process.

배경: 시공간 예약(예방형)은 lockstep 가정이라 비동기 실행 드리프트로 정면 교착이 남을 수 있다.
  그때 `_detect_deadlock_cycle`(감지) + `_resolve_deadlock`(한쪽 우회)이 backstop으로 푼다(수정 54).
  이 퍼저는 그 backstop이 랜덤 상황 전체에서 안 뚫리는지 검증한다.

불변식:
  [INV1] 감지 : 마주 선(둘 다 blocked·forward, 서로의 노드가 목표) 로봇쌍은 반드시 2-사이클로 감지
  [INV2] 완전성: 해소 실패했는데 독립 BFS로 우회로가 존재하면 = 버그(있는 길을 못 찾음)
  [INV3] 진짜해소: 해소 성공 주장 시, 재감지하면 그 사이클이 실제로 사라져야(가짜 해소 차단)

무한 반복. Ctrl+C 종료 시 최종 집계.
실행:  python3 -m virtual_test.deadlock_fuzz   /   python3 virtual_test/deadlock_fuzz.py
"""
import os
import sys
import time
import random
import argparse
import contextlib
import io
from collections import deque

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.config import Config                                  # noqa: E402
from server.planning.path_planner import PathPlanner              # noqa: E402
from server.managers.robot import RobotManager                    # noqa: E402
from server.managers.shelf import ShelfManager                    # noqa: E402
from server.managers.staging import StagingManager                # noqa: E402
from server.managers.task import TaskManager                      # noqa: E402
from server.core.request_handler import RequestHandler            # noqa: E402

DEG = {0: 0, 1: 90, 2: 180, 3: 270}   # _node_direction → heading


class _MockClient:
    def publish(self, *a, **k):
        pass


class _MockMqtt:
    def __init__(self):
        self.client = _MockClient()

    def connect(self):
        return True

    def disconnect(self):
        pass

    def is_connected(self):
        return True

    def subscribe(self, *a, **k):
        pass

    def publish_cmd(self, *a, **k):
        return True


@contextlib.contextmanager
def _quiet():
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def build_handler(cfg):
  with _quiet():
    pp = PathPlanner(cfg.map_file)
    rm = RobotManager(cfg)
    sm = ShelfManager(cfg.shelf_config_file)
    stg = StagingManager(
        sm.workstations,
        get_robot_node=lambda r: rm.get_robot(r).current_node if rm.get_robot(r) else None,
        get_robot_planned_path=lambda r: rm.get_robot(r).planned_path if rm.get_robot(r) else None,
    )
    tm = TaskManager(sm, pp)
    rh = RequestHandler(config=cfg, path_planner=pp, mqtt_publisher=_MockMqtt(),
                        robot_manager=rm, shelf_manager=sm, staging_manager=stg, task_manager=tm)
    for rb in rm.robots.values():
        rb.online = True
        rb.ever_seen = True
    return rh


def _astar_nodes(pp, a, goal):
    timed = pp.astar_with_time(a, goal, turn_penalty=0.3, max_time=60)
    return [n for n, _ in timed] if timed else None


def _bfs_reach(pp, start, goal, exclude):
    """start→goal 도달 가능? (exclude 노드는 통과 금지, start/goal은 허용) — 독립 BFS."""
    if start == goal:
        return True
    seen = {start}
    q = deque([start])
    while q:
        cur = q.popleft()
        for nxt, _ in pp.graph.get(cur, []):
            if nxt in seen:
                continue
            if nxt == exclude and nxt != goal:
                continue
            if nxt == goal:
                return True
            seen.add(nxt)
            q.append(nxt)
    return False


def run(cfg, log_every, fail_log, max_iters=0):
    pp0 = PathPlanner(cfg.map_file)
    non_shelf = [n for n in pp0.nodes if pp0.get_node_type(n) != "S"]

    n = fails = skipped = 0
    resolved_ok = unresolvable_legit = 0
    t0 = time.time()

    print("=" * 90, flush=True)
    print("  교착 해소 퍼저 — 정면(head-on) 교착 감지/해소 회귀 검사 (수정 54) | server.main 무관", flush=True)
    print("  [INV1]감지 [INV2]완전성(우회로 있으면 반드시 해소) [INV3]진짜해소(재감지 시 사라짐)", flush=True)
    print("=" * 90, flush=True)

    def _final(reason):
        el = time.time() - t0
        status = "✅ 교착 항상 해소 (FAIL 0건)" if fails == 0 else f"❌ FAIL {fails}건 — 교착 backstop 결함"
        print("\n" + "=" * 90, flush=True)
        print(f"  [{reason}] 총 {n:,}건 | {status} | 해소성공 {resolved_ok:,} · "
              f"우회로없어정상실패 {unresolvable_legit:,} · skip {skipped:,}", flush=True)
        print(f"  경과 {el:.1f}s | {n/max(el,1e-9):,.0f} it/s", flush=True)
        print("=" * 90, flush=True)

    def _fail(msg):
        nonlocal fails
        fails += 1
        line = f"[FAIL #{fails}] iter={n} {msg}"
        print(line, flush=True)
        with open(fail_log, "a") as f:
            f.write(line + "\n")

    try:
        while True:
            n += 1
            rh = build_handler(cfg)
            pp = rh.path_planner
            rm = rh.robot_manager
            rids = sorted(rm.robots.keys())
            if len(rids) < 2:
                skipped += 1
                continue
            r1id, r2id = rids[0], rids[1]

            # 마주 선 간선 (a,b) — 둘 다 비-선반, 인접
            a = random.choice(non_shelf)
            nbrs = [x for x, _ in pp.graph.get(a, []) if pp.get_node_type(x) != "S"]
            if not nbrs:
                skipped += 1
                continue
            b = random.choice(nbrs)

            ws_nodes = set(rh.staging_manager.corridors.keys())   # staging 유발 목적지 회피
            # 목적지에서 {a,b} 제외: goal==상대노드면 그 노드를 피할 수 없어(목적지라) 정상적으로
            # 우회 불가 → 교착 아님(상대가 비켜야 풀림). 진짜 head-on은 목표가 서로 '너머'일 때다.
            goal_pool = [g for g in pp.nodes if g not in ws_nodes and g not in (a, b)]
            if not goal_pool:
                skipped += 1
                continue
            goal1 = random.choice(goal_pool)
            goal2 = random.choice(goal_pool)
            p1 = _astar_nodes(pp, a, goal1) or [a]
            p2 = _astar_nodes(pp, b, goal2) or [b]

            r1, r2 = rm.get_robot(r1id), rm.get_robot(r2id)
            r1.current_node = a; r1.heading = DEG[pp._node_direction(a, b)]
            r1.heading_initialized = True; r1.planned_path = p1; r1.command_queue = ["forward"]
            r2.current_node = b; r2.heading = DEG[pp._node_direction(b, a)]
            r2.heading_initialized = True; r2.planned_path = p2; r2.command_queue = ["forward"]

            # [INV1] 감지
            with _quiet():
                cycle = rh._detect_deadlock_cycle()
            if cycle is None or set(cycle) != {r1id, r2id}:
                _fail(f"[INV1] 정면교착 미감지 a={a} b={b} (감지결과 {cycle})")
                continue

            # 해소
            with _quiet():
                resolved = rh._resolve_deadlock(list(cycle))

            if resolved:
                # [INV3] 진짜 해소 — 재감지 시 같은 사이클이 사라져야
                with _quiet():
                    again = rh._detect_deadlock_cycle()
                if again is not None and set(again) == {r1id, r2id}:
                    _fail(f"[INV3] 해소 성공 주장했으나 교착 그대로 a={a} b={b} goal1={goal1}")
                else:
                    resolved_ok += 1
            else:
                # [INV2] 완전성 — 우회로가 실제 있으면 못 푼 것 = 버그
                bypass1 = _bfs_reach(pp, a, goal1, exclude=b)
                bypass2 = _bfs_reach(pp, b, goal2, exclude=a)
                if bypass1 or bypass2:
                    who = f"AGV1 {a}→{goal1}(excl {b})={bypass1}, AGV2 {b}→{goal2}(excl {a})={bypass2}"
                    _fail(f"[INV2] 우회로 있는데 해소 실패 — {who}")
                else:
                    unresolvable_legit += 1   # 정말 길이 없음 = 정상(양쪽 다 막다른 목적지)

            if n % log_every == 0:
                el = time.time() - t0
                status = "✅ 무결점" if fails == 0 else f"❌ FAIL {fails}건"
                print(f"[hb] {n:,}건 | {status} | 해소 {resolved_ok:,}·우회로없음 {unresolvable_legit:,}"
                      f"·skip {skipped:,} | {n/el:,.0f} it/s | {el:.0f}s", flush=True)

            if max_iters and n >= max_iters:
                _final("완료")
                return fails
    except KeyboardInterrupt:
        _final("Ctrl+C 중단")
        return fails


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--log-every", type=int, default=2000)
    p.add_argument("--max-iters", type=int, default=0)
    p.add_argument("--fail-log", default=os.path.join(PROJECT_ROOT, "deadlock_fuzz_fails.log"))
    a = p.parse_args()
    run(Config(), a.log_every, a.fail_log, a.max_iters)
