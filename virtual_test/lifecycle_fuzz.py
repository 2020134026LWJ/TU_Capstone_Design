#!/usr/bin/env python3
"""예약 lifecycle 퍼저 — '죽은 예약'(수정 74류) 회귀 검사.

★ path_stress.py 와 다른 층: 이건 실제 RequestHandler(handler 층)를 몰아서,
  로봇이 IDLE로 끝났을 때 예약이 제대로 청소되는지 본다. server.main/MQTT 없이 in-process.

왜 필요: path_stress.py 는 순수 planning(astar+commit)만 봐서, 예약을 '언제 청소하나'(handler
  lifecycle)를 못 본다. 수정 74 버그(주인 없는 죽은 예약이 쌓여 A*가 유령 장애물을 +2칸 피함)는
  planning 층이 아니라 handler 층(on_idle/advance/release 미호출)에 있었다. 그래서 정적 테스트로는
  안 잡힌다 → 이 퍼저가 그 사각지대를 메운다.

검사 불변식:
  [I] IDLE 청소   : 이동(예약 커밋)한 로봇을 IDLE로 보내면 → 그 로봇의 timed 예약(cell/edge)이 0이어야.
                    (수정 74 전: on_idle 청소가 없어 죽은 예약이 남음 → 이 검사가 FAIL)
  [W] 낭비 없음   : 죽은 예약이 남아 다른 로봇 경로를 낭비시키지 않는지 — '예약이 정상 청소된
                    상태에서의 최적비용'과 비교해 초과분이 없어야.

무한 반복. Ctrl+C 종료 시 최종 집계.
실행:  python3 -m virtual_test.lifecycle_fuzz   /   python3 virtual_test/lifecycle_fuzz.py
"""
import os
import sys
import time
import random
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from server.config import Config                                  # noqa: E402
from server.planning.path_planner import PathPlanner              # noqa: E402
from server.managers.robot import RobotManager, RobotStatus       # noqa: E402
from server.managers.shelf import ShelfManager                    # noqa: E402
from server.managers.staging import StagingManager                # noqa: E402
from server.managers.task import TaskManager                      # noqa: E402
from server.core.request_handler import RequestHandler            # noqa: E402


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


import contextlib
import io


@contextlib.contextmanager
def _quiet():
    """빌드 중 매니저들이 찍는 초기화 로그를 삼킴 (매 반복 시끄러움 방지)."""
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
        rb.heading = 0
        rb.heading_initialized = True
    return rh


def _cells_of(res, rid):
    return [k for k, v in res._cells.items() if v == rid]


def _edges_of(res, rid):
    return [k for k, v in res._edges.items() if v == rid]


def run(cfg, log_every, fail_log, max_iters=0):
    pp0 = PathPlanner(cfg.map_file)
    move_goals = [n for n in pp0.nodes if pp0.get_node_type(n) == "M"]   # 선반/작업대 목적지 회피(staging 배제)

    n = fails = skipped = 0
    n_idle = n_waste = 0
    t0 = time.time()

    print("=" * 88, flush=True)
    print("  예약 lifecycle 퍼저 — 죽은 예약(수정 74류) 회귀 검사 | server.main 무관", flush=True)
    print(f"  검사: [I]IDLE청소(이동→IDLE→예약0)  [W]낭비없음(죽은예약이 남 경로 안 늘림)", flush=True)
    print("=" * 88, flush=True)

    def _final(reason):
        el = time.time() - t0
        status = "✅ 죽은 예약 없음 (FAIL 0건)" if fails == 0 else f"❌ FAIL {fails}건 — 죽은 예약 검출"
        print("\n" + "=" * 88, flush=True)
        print(f"  [{reason}] 총 {n:,}건 (I:{n_idle:,} W:{n_waste:,}, skip {skipped:,}) | {status} "
              f"| {el:.1f}s | {n/max(el,1e-9):,.0f} it/s", flush=True)
        print("=" * 88, flush=True)

    try:
        while True:
            n += 1
            rh = build_handler(cfg)
            res = rh.reservation
            rids = list(rh.robot_manager.robots.keys())
            rid = random.choice(rids)
            rb = rh.robot_manager.get_robot(rid)
            start = rb.current_node
            goal = random.choice([g for g in move_goals if g != start])

            problem = None
            if random.random() < 0.7:
                # ─ [I] IDLE 청소 ─
                n_idle += 1
                with _quiet():
                    rh._plan_and_publish_move(rid, start, goal)
                if not _cells_of(res, rid):
                    skipped += 1            # 경로 못 짬/즉시도착 → 이번은 건너뜀
                    continue
                with _quiet():
                    rh.robot_manager.set_robot_status(rid, RobotStatus.MOVING_TO_SHELF)
                    rh.robot_manager.set_robot_status(rid, RobotStatus.IDLE)
                lc, le = _cells_of(res, rid), _edges_of(res, rid)
                if lc or le:
                    problem = (f"[I] 죽은 예약! IDLE 로봇 {rid} 가 cell {len(lc)}개·edge {len(le)}개 "
                               f"잔존 (홈{start}→{goal}) 예:{(lc[:2]+le[:2])}")
            else:
                # ─ [W] 낭비 없음 ─ : 로봇A 이동+IDLE(청소돼야) 후, 로봇B가 A의 옛길을 낭비없이 지나야
                n_waste += 1
                rid_b = random.choice([r for r in rids if r != rid]) if len(rids) > 1 else rid
                with _quiet():
                    rh._plan_and_publish_move(rid, start, goal)
                    rh.robot_manager.set_robot_status(rid, RobotStatus.MOVING_TO_SHELF)
                    rh.robot_manager.set_robot_status(rid, RobotStatus.IDLE)   # A는 청소돼야 함
                if not _cells_of(res, rid):
                    # A가 청소된 상태 — B가 A의 옛길을 낭비 없이 지나는지
                    rb2 = rh.robot_manager.get_robot(rid_b)
                    s2 = rb2.current_node
                    g2 = random.choice([g for g in move_goals if g != s2])
                    timed_dirty = pp0.astar_with_time(s2, g2, reservation=res, rid=rid_b,
                                                      turn_penalty=0.3, max_time=60)
                    clean = ReservationService_clean()
                    timed_clean = pp0.astar_with_time(s2, g2, reservation=clean, rid=rid_b,
                                                      turn_penalty=0.3, max_time=60)
                    if timed_dirty and timed_clean and len(timed_dirty) > len(timed_clean):
                        problem = (f"[W] 경로 낭비! 로봇A({rid}) IDLE 후 죽은 예약이 남아 "
                                   f"로봇B({rid_b}) {s2}→{g2} 경로가 {len(timed_clean)-1}→"
                                   f"{len(timed_dirty)-1}칸으로 늘어남")
                else:
                    # A가 청소 안 됨 = [I]에서 이미 잡히는 케이스. 여기선 낭비 여부만 보므로 skip 처리
                    skipped += 1
                    continue

            if problem:
                fails += 1
                line = f"[FAIL #{fails}] iter={n} {problem}"
                print(line, flush=True)
                with open(fail_log, "a") as f:
                    f.write(line + "\n")

            if n % log_every == 0:
                el = time.time() - t0
                status = "✅ 무결점" if fails == 0 else f"❌ FAIL {fails}건"
                print(f"[hb] {n:,}건 (I:{n_idle:,} W:{n_waste:,} skip:{skipped:,}) | {status} | "
                      f"{n/el:,.0f} it/s | {el:.0f}s", flush=True)

            if max_iters and n >= max_iters:
                _final("완료")
                return fails
    except KeyboardInterrupt:
        _final("Ctrl+C 중단")
        return fails


def ReservationService_clean():
    from server.planning.reservation_service import ReservationService
    return ReservationService()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--log-every", type=int, default=2000)
    p.add_argument("--max-iters", type=int, default=0)
    p.add_argument("--fail-log", default=os.path.join(PROJECT_ROOT, "lifecycle_fuzz_fails.log"))
    a = p.parse_args()
    run(Config(), a.log_every, a.fail_log, a.max_iters)
