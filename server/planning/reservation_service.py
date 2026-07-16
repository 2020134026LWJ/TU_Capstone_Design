"""
시공간 예약 단일 진실 (REFACTOR F Phase 2).

"AGV X가 t 시각에 노드 N에 있다 / 노드 A→B를 t 시각에 통과한다" — 이 정보의
유일한 출처. A*는 is_free()로 충돌 회피, mixin 핸들러는 commit/release로 갱신.

기존의 흩어진 상태 (_reserved_nodes, _goal_locked_robots, _deferred_goals,
robot.planned_path '진실' 격)를 단일 자료구조로 흡수.

invariant:
- I1 (단일 진실): 미래 점유는 ReservationService에만. 그림자 상태 금지.
- I2 (plan 시점 차단): commit 실패 = 충돌 가능 → 호출자가 새 plan 시도 (또는 wait).
- I3 (atomic): commit 충돌 시 기존 예약 손실 X (release는 conflict-free 확인 후).
- I4 (자동 정리): advance(rid, node, t) → (rid, time' < t) 예약 자동 해제.
- I5 (callback): release(rid) 시 등록된 콜백 호출 → 대기 AGV 깨우기.

상세: server/REFACTOR_F.md Phase 2
"""
from typing import Callable, Dict, List, Optional, Tuple


CellKey = Tuple[int, int]          # (node, time)
EdgeKey = Tuple[int, int, int]     # (from, to, time)


class ReservationService:
    """시공간 cell + edge 예약 관리.

    cell 예약: 특정 시점 t에 노드 N을 점유.
    edge 예약: 시점 t에 A→B로 통과 — swap collision(A↔B 동시 교차) 차단용.
    indefinite 예약: 시간 무관 영구 점유 (goal-lock 대체).
    """

    def __init__(self):
        self._cells: Dict[CellKey, int] = {}
        self._edges: Dict[EdgeKey, int] = {}
        # rid → [("cell", CellKey), ("edge", EdgeKey), ("indef", node)] 역색인
        self._by_rid: Dict[int, List[Tuple[str, tuple]]] = {}
        self._indefinite_by_node: Dict[int, int] = {}  # node → rid
        self._release_callbacks: List[Callable[[int], None]] = []
        # 수정 85: 회전 footprint. 든 로봇이 제자리 회전하면 선반이 대각(s√2)으로
        # 부풀어 직교 이웃칸을 '회전하는 동안' 점유한다 → 그 물리적 사실을 예약으로 표현.
        # 회전 dispatch 시 이웃칸 예약, 회전 ack 시 해제. 다른 로봇의 forward가 그 칸에
        # 진입하려 하면 대기(is_turn_locked). indefinite(코리도, 단일소유)와 별개 자료구조:
        # 회전은 시간축 없이 '지금 회전 중'만 표현하면 되고, 코리도 소유권을 건드리면 안 됨.
        self._turn_footprint: Dict[int, int] = {}          # node → rid (회전 중 이웃 점유)
        self._turn_footprint_by_rid: Dict[int, List[int]] = {}

    # ─── commit / release ───

    def commit(
        self, rid: int, path: List[int], start_time: int = 0, dwell: int = 0
    ) -> bool:
        """path 전체를 예약. 충돌 시 False (기존 rid 예약 손실 없음).

        path = [n0, n1, ..., nk]
        예약: cell (ni, start_time+i ~ start_time+i+dwell), edge (ni, n(i+1), start_time+i)
        swap collision: (A→B, t)와 (B→A, t) 동시 예약 차단.

        Args:
            dwell: 각 노드를 점유하는 step 수의 추가 마진 (기본 0).
                   1이면 (ni, t) + (ni, t+1) 둘 다 점유 — legacy timing 버퍼 유지용.

        rid 자신의 기존 예약은 충돌로 간주하지 않음 (어차피 곧 release).
        """
        if not path:
            return True

        # 1차 — 충돌 dry-run (자기 자신 예약은 통과)
        for i, node in enumerate(path):
            base_t = start_time + i
            for dt in range(dwell + 1):
                t = base_t + dt
                owner = self._cells.get((node, t))
                if owner is not None and owner != rid:
                    return False
            ind_owner = self._indefinite_by_node.get(node)
            if ind_owner is not None and ind_owner != rid:
                return False

        for i in range(len(path) - 1):
            t = start_time + i
            forward_key = (path[i], path[i + 1], t)
            owner = self._edges.get(forward_key)
            if owner is not None and owner != rid:
                return False
            swap_key = (path[i + 1], path[i], t)
            owner = self._edges.get(swap_key)
            if owner is not None and owner != rid:
                return False

        # 2차 — 본 commit (기존 rid의 cell/edge release 후 새로 박음).
        # indefinite(corridor 점유)는 replan 중에도 유지 (Phase 4.5.0).
        self.release(rid, fire_callbacks=False, keep_indefinite=True)

        for i, node in enumerate(path):
            base_t = start_time + i
            for dt in range(dwell + 1):
                cell_key = (node, base_t + dt)
                # 같은 cell 중복 등록 방지 (인접 step 노드 같을 때)
                if self._cells.get(cell_key) == rid:
                    continue
                self._cells[cell_key] = rid
                self._by_rid.setdefault(rid, []).append(("cell", cell_key))

        for i in range(len(path) - 1):
            t = start_time + i
            edge_key = (path[i], path[i + 1], t)
            self._edges[edge_key] = rid
            self._by_rid.setdefault(rid, []).append(("edge", edge_key))

        return True

    def release(
        self, rid: int, fire_callbacks: bool = True, keep_indefinite: bool = False
    ) -> None:
        """rid의 cell + edge 예약 해제 (indefinite은 keep_indefinite로 선택).

        Args:
            fire_callbacks: True면 등록된 on_release 콜백 호출 (내부 commit 재실행
                            시에는 False로 중복 발화 방지).
            keep_indefinite: True면 indefinite 예약은 유지하고 cell/edge만 해제.
                             snapshot resync / replan이 corridor 영구 점유(Phase 4.5)를
                             지우지 않도록 함. 기본 False = 전부 해제 (퇴출 시).
        """
        keys = self._by_rid.pop(rid, [])
        kept: List[Tuple[str, tuple]] = []
        for kind, key in keys:
            if kind == "cell":
                if self._cells.get(key) == rid:
                    del self._cells[key]
            elif kind == "edge":
                if self._edges.get(key) == rid:
                    del self._edges[key]
            elif kind == "indef":
                if keep_indefinite:
                    kept.append((kind, key))
                    continue
                if self._indefinite_by_node.get(key) == rid:
                    del self._indefinite_by_node[key]
        if kept:
            self._by_rid[rid] = kept

        if fire_callbacks:
            for cb in self._release_callbacks:
                cb(rid)

    # ─── query ───

    def is_free(self, node: int, time: int, exclude_rid: Optional[int] = None) -> bool:
        """(node, time)이 예약되지 않았는지 확인. exclude_rid 자신의 예약은 무시."""
        owner = self._cells.get((node, time))
        if owner is not None and owner != exclude_rid:
            return False
        ind_owner = self._indefinite_by_node.get(node)
        if ind_owner is not None and ind_owner != exclude_rid:
            return False
        return True

    def is_edge_free(
        self, from_node: int, to_node: int, time: int, exclude_rid: Optional[int] = None
    ) -> bool:
        """edge (from→to, time)와 swap (to→from, time) 모두 비어있는지."""
        for key in [(from_node, to_node, time), (to_node, from_node, time)]:
            owner = self._edges.get(key)
            if owner is not None and owner != exclude_rid:
                return False
        return True

    # ─── advance (마커 도착 시 자동 정리) ───

    def advance(self, rid: int, current_node: int, t: int) -> None:
        """rid가 (current_node, t)에 도달 → rid 소유 예약 중 time < t 자동 해제.

        indefinite는 영향 X. current_node 자체의 예약도 t 시점은 유지
        (도착했지만 아직 점유 중이라는 의미).
        """
        keys = self._by_rid.get(rid, [])
        keep: List[Tuple[str, tuple]] = []
        for kind, key in keys:
            if kind == "cell" and key[1] < t:
                if self._cells.get(key) == rid:
                    del self._cells[key]
                continue
            if kind == "edge" and key[2] < t:
                if self._edges.get(key) == rid:
                    del self._edges[key]
                continue
            keep.append((kind, key))
        if keep:
            self._by_rid[rid] = keep
        else:
            self._by_rid.pop(rid, None)

    # ─── indefinite (corridor 점유 / Phase 4.5) ───

    def is_corridor_held(self, node: int, exclude_rid: Optional[int] = None) -> bool:
        """node가 indefinite으로 점유됐는지 (= corridor 점유 여부).

        is_free와 달리 cell/edge(경로 예약)는 무시하고 indefinite만 본다.
        staging이 "회랑 비었나?"를 판단할 때 사용 (Phase 4.5.2).
        """
        owner = self._indefinite_by_node.get(node)
        return owner is not None and owner != exclude_rid

    def corridor_owner(self, node: int) -> Optional[int]:
        """node를 indefinite으로 점유한 rid (없으면 None). corridor 주인 질의 (Phase 4.5.3)."""
        return self._indefinite_by_node.get(node)

    def reserve_indefinite(self, rid: int, node: int) -> None:
        """rid가 node에 시간 무관 점유. 같은 노드 재호출은 idempotent.

        다른 rid가 점유 중이면 이전 소유자 엔트리를 정리하고 이전 (corridor transfer).
        """
        cur = self._indefinite_by_node.get(node)
        if cur == rid:
            return
        if cur is not None:
            self._drop_indef_entry(cur, node)  # 이전 소유자 정리 (transfer)
        self._indefinite_by_node[node] = rid
        self._by_rid.setdefault(rid, []).append(("indef", node))

    def release_indefinite_node(self, node: int, fire_callbacks: bool = False) -> None:
        """node의 indefinite 점유만 해제 (해당 rid의 cell/edge 예약은 보존).

        corridor 점유자가 퇴출할 때 사용 — 점유 자원(WS 노드)만 풀고,
        퇴출 경로 예약은 그대로 둠. fire_callbacks=True면 on_release 콜백 발화 (wake-up).
        """
        rid = self._indefinite_by_node.get(node)
        if rid is None:
            return
        self._drop_indef_entry(rid, node)
        if fire_callbacks:
            for cb in self._release_callbacks:
                cb(rid)

    def _drop_indef_entry(self, rid: int, node: int) -> None:
        """(rid, node) indefinite 엔트리를 _indefinite_by_node + _by_rid에서 제거."""
        if self._indefinite_by_node.get(node) == rid:
            del self._indefinite_by_node[node]
        entries = self._by_rid.get(rid)
        if entries:
            kept = [e for e in entries if not (e[0] == "indef" and e[1] == node)]
            if kept:
                self._by_rid[rid] = kept
            else:
                self._by_rid.pop(rid, None)

    # ─── turn footprint (수정 85: 든 채 회전 시 이웃칸 일시 점유) ───

    def reserve_turn_footprint(self, rid: int, nodes: List[int]) -> None:
        """rid가 든 채 회전하는 동안 이웃 nodes를 일시 점유. 회전 ack 시 release.

        같은 rid 재호출은 기존 footprint 교체(idempotent). 이미 다른 rid가 점유한
        노드는 건너뛴다 — 그 노드는 그 로봇이 회전 중이라 어차피 진입 불가.
        (cell/edge/indefinite 예약과 독립: 회전은 '지금 회전 중'만 표현하면 됨.)
        """
        self.release_turn_footprint(rid)
        held: List[int] = []
        for n in nodes:
            if self._turn_footprint.get(n) is None:
                self._turn_footprint[n] = rid
                held.append(n)
        if held:
            self._turn_footprint_by_rid[rid] = held

    def release_turn_footprint(self, rid: int) -> None:
        """rid의 회전 footprint 점유 전부 해제 (회전 완료/취소/이탈 시)."""
        for n in self._turn_footprint_by_rid.pop(rid, []):
            if self._turn_footprint.get(n) == rid:
                del self._turn_footprint[n]

    def is_turn_locked(self, node: int, exclude_rid: Optional[int] = None) -> bool:
        """node가 다른 로봇의 회전 footprint로 점유됐는지 (forward 진입 차단용)."""
        owner = self._turn_footprint.get(node)
        return owner is not None and owner != exclude_rid

    def turn_lock_owner(self, node: int) -> Optional[int]:
        """node를 회전 footprint로 점유한 rid (없으면 None)."""
        return self._turn_footprint.get(node)

    # ─── callbacks ───

    def on_release(self, callback: Callable[[int], None]) -> None:
        """release(rid) 발생 시 호출될 콜백 등록. 인자: 해제된 rid."""
        self._release_callbacks.append(callback)
