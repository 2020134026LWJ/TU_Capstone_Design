"""작업대 스테이징 관리 모듈
- 작업대 회랑(corridor) 진입/퇴출 관리
- 충돌 방지를 위한 AGV 대기 큐
"""

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

STAGING_TIMEOUT = 30  # PARAM: 스테이징 corridor 점유 타임아웃 (초). 마커 인식 실패 시 강제 해제까지 대기


class CorridorState(Enum):
    """회랑 상태"""
    FREE = "free"
    OCCUPIED = "occupied"


@dataclass
class StagedAGV:
    """대기 중인 AGV 정보"""
    rid: int
    staging_node: int
    target_ws: int
    staged_at: float = field(default_factory=time.time)


@dataclass
class PreemptResult:
    """gateway 도착 시 corridor 점유 이전 결과"""
    target_ws: int
    previous_rid: int
    new_rid: int


@dataclass
class CorridorInfo:
    """작업대 회랑 정보"""
    ws_node: int
    gateway_node: int
    staging_node: int
    trigger_node: int
    state: CorridorState = CorridorState.FREE
    occupying_rid: Optional[int] = None
    occupied_at: float = 0.0
    queue: deque = field(default_factory=deque)
    is_exiting: bool = False  # mark_exiting() 호출 이후 퇴출 중 여부


class StagingManager:
    """작업대 스테이징 관리자"""

    def __init__(self, workstations: Dict[int, Dict],
                 get_robot_node: Optional[Callable[[int], Optional[int]]] = None,
                 get_robot_planned_path: Optional[Callable[[int], Optional[List[int]]]] = None):
        # try_preempt_at_gateway에서 점유자 위치/경로 조회용 콜백
        self._get_robot_node = get_robot_node
        self._get_robot_planned_path = get_robot_planned_path
        self.corridors: Dict[int, CorridorInfo] = {}

        # ws_node(33,34) → gateway, staging, trigger 매핑
        for ws_node_str, ws_info in workstations.items():
            ws_node = int(ws_node_str)
            gateway = ws_info.get("gateway_node")
            staging = ws_info.get("staging_node")
            trigger = ws_info.get("trigger_node")

            if gateway and staging and trigger:
                self.corridors[ws_node] = CorridorInfo(
                    ws_node=ws_node,
                    gateway_node=gateway,
                    staging_node=staging,
                    trigger_node=trigger,
                )

        # trigger_node → ws_node 역매핑 (마커 인식 시 빠른 조회)
        self._trigger_to_ws: Dict[int, int] = {}
        for ws_node, corridor in self.corridors.items():
            self._trigger_to_ws[corridor.trigger_node] = ws_node

        # staging_node → ws_node 역매핑
        self._staging_to_ws: Dict[int, int] = {}
        for ws_node, corridor in self.corridors.items():
            self._staging_to_ws[corridor.staging_node] = ws_node

        # 타임아웃으로 해제된 대기 AGV 목록 (request_handler가 드레인해서 이동 명령 발행)
        self.pending_timeout_releases: List[StagedAGV] = []

        print(f"[StagingManager] Initialized {len(self.corridors)} corridors")

    def get_ws_for_staging_node(self, node: int) -> Optional[int]:
        """staging 노드 → 해당 작업대 노드 반환 (큐 기반 우선 — gateway-staging 포함)"""
        # 동적 조회: 현재 누군가가 이 노드에서 staging 중이면 그 ws 반환
        for ws_node, corridor in self.corridors.items():
            if any(s.staging_node == node for s in corridor.queue):
                return ws_node
        # 정적 fallback (기본 staging_node)
        return self._staging_to_ws.get(node)

    def should_stage(self, ws_node: int, incoming_rid: int, is_forwarding: bool = False) -> Optional[int]:
        """작업대로 가려는 AGV가 스테이징 필요한지 확인

        Args:
            ws_node: 목적지 작업대 노드
            incoming_rid: 진입 시도 AGV ID
            is_forwarding: 포워딩 여부 — True면 corridor 바깥 staging_node 대신
                           진입 경로 위 gateway_node에서 대기 (선반 들고 멀리 우회 방지)

        Returns:
            None: 스테이징 불필요 (바로 진입 가능)
            int: 대기 노드 (포워딩이면 gateway, 일반이면 staging)
        """
        corridor = self.corridors.get(ws_node)
        if not corridor:
            return None

        # 타임아웃 체크
        self._check_timeout(corridor)

        if corridor.state == CorridorState.FREE:
            # 회랑 비어있음 → 바로 진입, 회랑 점유
            corridor.state = CorridorState.OCCUPIED
            corridor.occupying_rid = incoming_rid
            corridor.occupied_at = time.time()
            print(f"[StagingManager] W{ws_node}: corridor OCCUPIED by AGV-{incoming_rid} (entering)")
            return None
        elif corridor.occupying_rid == incoming_rid:
            # 이미 권한 있음 (스테이징에서 해제된 AGV)
            print(f"[StagingManager] W{ws_node}: AGV-{incoming_rid} already authorized")
            return None
        else:
            # 회랑 점유 중 → 대기 노드 결정 (포워딩=gateway, 일반=staging)
            wait_node = corridor.gateway_node if is_forwarding else corridor.staging_node
            wait_label = "gateway-staging" if is_forwarding else "staging"
            print(f"[StagingManager] W{ws_node}: corridor busy (AGV-{corridor.occupying_rid}), "
                  f"AGV-{incoming_rid} {wait_label} at node {wait_node}")
            return wait_node

    def add_staged_agv(self, ws_node: int, rid: int, staging_node: int):
        """대기 큐에 AGV 추가"""
        corridor = self.corridors.get(ws_node)
        if not corridor:
            return

        staged = StagedAGV(rid=rid, staging_node=staging_node, target_ws=ws_node)
        corridor.queue.append(staged)
        print(f"[StagingManager] W{ws_node}: AGV-{rid} queued at node {staging_node} "
              f"(queue size: {len(corridor.queue)})")

    def is_staged_agv(self, node: int, rid: int) -> bool:
        """해당 노드에 도착한 AGV가 스테이징 대기 중인지 확인 (큐 기반 — gateway 포함)"""
        for corridor in self.corridors.values():
            for s in corridor.queue:
                if s.rid == rid and s.staging_node == node:
                    return True
        return False

    def try_preempt_at_gateway(self, gateway_node: int, claimant_rid: int) -> Optional[PreemptResult]:
        """gateway에 도착한 staged AGV가 점유자보다 corridor에 가까우면 점유 이전.

        조건: 점유자가 corridor area({ws_node, gateway_node}) 밖에 있어야 함 (미진입).

        Returns:
            PreemptResult: 선점 성공 (호출자가 이전 점유자 재라우팅 책임)
            None: 선점 불가
        """
        # gateway_node로 corridor 찾기
        corridor = next((c for c in self.corridors.values()
                         if c.gateway_node == gateway_node), None)
        if corridor is None or corridor.state != CorridorState.OCCUPIED:
            return None

        owner_rid = corridor.occupying_rid
        if owner_rid is None or owner_rid == claimant_rid:
            return None

        # claimant가 큐에 staged 상태인지 확인 (정상 호출 경로 보장)
        if not any(s.rid == claimant_rid and s.staging_node == gateway_node
                   for s in corridor.queue):
            return None

        # 점유자 현재 노드 확인 — 미주입이면 보수적으로 거부
        if self._get_robot_node is None:
            return None
        owner_node = self._get_robot_node(owner_rid)
        corridor_area = {corridor.ws_node, corridor.gateway_node}
        if owner_node is None or owner_node in corridor_area:
            return None  # 점유자 이미 진입 → 선점 불가

        # 점유자가 corridor로 in-flight 진입 중인지 (다음 계획 노드가 corridor area) 확인
        if self._get_robot_planned_path is not None:
            owner_path = self._get_robot_planned_path(owner_rid)
            if owner_path and len(owner_path) >= 2 and owner_path[1] in corridor_area:
                return None  # owner가 corridor로 진입 중 (cmd in-flight) → 선점 불가

        # 선점 가능 — 점유 이전
        previous_rid = owner_rid
        corridor.occupying_rid = claimant_rid
        corridor.occupied_at = time.time()
        corridor.is_exiting = False
        corridor.queue = deque(s for s in corridor.queue if s.rid != claimant_rid)
        print(f"[StagingManager] W{corridor.ws_node}: corridor PREEMPTED — "
              f"AGV-{previous_rid} → AGV-{claimant_rid} (owner at node {owner_node})")
        return PreemptResult(target_ws=corridor.ws_node,
                             previous_rid=previous_rid,
                             new_rid=claimant_rid)

    def handle_marker_trigger(self, rid: int, marker_id: int) -> Optional[StagedAGV]:
        """마커 인식 이벤트 → 트리거 노드면 대기 AGV 해제

        Args:
            rid: 마커를 인식한 AGV의 ID (퇴출 중인 AGV)
            marker_id: 인식된 마커 ID (= 노드 ID)

        Returns:
            StagedAGV: 해제된 대기 AGV 정보 (진입 시킬 AGV)
            None: 트리거가 아니거나 대기 AGV 없음
        """
        ws_node = self._trigger_to_ws.get(marker_id)
        if not ws_node:
            return None

        corridor = self.corridors.get(ws_node)
        if not corridor:
            return None

        # 트리거 인식 = 퇴출 AGV가 게이트웨이를 벗어남
        print(f"[StagingManager] W{ws_node}: AGV-{rid} passed trigger node {marker_id}")

        return self.release_corridor_without_trigger(ws_node, rid)

    def release_corridor_without_trigger(self, ws_node: int, rid: int) -> Optional["StagedAGV"]:
        """포워딩 시 소스 회랑 즉시 해제 (트리거 노드 없이)

        RETURN_SHELF는 트리거 ArUco로 회랑을 해제하지만,
        FORWARD_SHELF는 트리거 노드를 지나지 않을 수 있으므로 직접 해제.
        포워딩 경로를 _robot_planned_paths에 등록한 뒤 호출해야 함.
        """
        corridor = self.corridors.get(ws_node)
        if not corridor:
            return None

        if corridor.occupying_rid != rid:
            print(f"[StagingManager] W{ws_node}: release_without_trigger skip "
                  f"(occupying={corridor.occupying_rid}, rid={rid})")
            return None

        if corridor.queue:
            released = corridor.queue.popleft()
            corridor.state = CorridorState.OCCUPIED
            corridor.occupying_rid = released.rid
            corridor.occupied_at = time.time()
            corridor.is_exiting = False
            print(f"[StagingManager] W{ws_node}: forwarding release → "
                  f"AGV-{released.rid} corridor transferred (queue remaining: {len(corridor.queue)})")
            return released
        else:
            corridor.state = CorridorState.FREE
            corridor.occupying_rid = None
            corridor.is_exiting = False
            print(f"[StagingManager] W{ws_node}: forwarding release → corridor FREE")
            return None

    def mark_exiting(self, ws_node: int, rid: int):
        """AGV가 작업대에서 퇴출 시작 (pick_complete → return/forward)"""
        corridor = self.corridors.get(ws_node)
        if not corridor:
            return

        corridor.state = CorridorState.OCCUPIED
        corridor.occupying_rid = rid
        corridor.occupied_at = time.time()
        corridor.is_exiting = True
        print(f"[StagingManager] W{ws_node}: AGV-{rid} exiting")

    def check_position_release(self, rid: int, node: int) -> Optional["StagedAGV"]:
        """위치 기반 회랑 자동 해제.

        퇴출 중인(is_exiting=True) 로봇이 회랑 구역(WS + 게이트웨이) 밖으로
        이동하면 즉시 회랑을 해제한다. ArUco 트리거가 경로에 없는 경우에도 동작.

        Returns:
            StagedAGV: 해제된 대기 AGV (이동 명령 발행 필요)
            None: 해제 없음 또는 해제 후 대기 AGV 없음 (FREE)
        """
        for ws_node, corridor in self.corridors.items():
            if (corridor.occupying_rid == rid
                    and corridor.state == CorridorState.OCCUPIED
                    and corridor.is_exiting):
                corridor_area = {corridor.ws_node, corridor.gateway_node}
                if node not in corridor_area:
                    print(f"[StagingManager] W{ws_node}: position-based release, "
                          f"AGV-{rid} moved to node {node}")
                    return self.release_corridor_without_trigger(ws_node, rid)
        return None

    def _check_timeout(self, corridor: CorridorInfo):
        """타임아웃 체크 - ArUco 인식 실패 시 강제 해제"""
        if corridor.state != CorridorState.OCCUPIED:
            return

        elapsed = time.time() - corridor.occupied_at
        if elapsed > STAGING_TIMEOUT:
            print(f"[StagingManager] W{corridor.ws_node}: TIMEOUT ({elapsed:.0f}s), "
                  f"forcing corridor FREE (was AGV-{corridor.occupying_rid})")
            if corridor.queue:
                released = corridor.queue.popleft()
                corridor.occupying_rid = released.rid
                corridor.occupied_at = time.time()
                print(f"[StagingManager] W{corridor.ws_node}: timeout-releasing AGV-{released.rid}")
                # 이동 명령은 pending_timeout_releases에 저장 → request_handler가 처리
                self.pending_timeout_releases.append(released)
            else:
                corridor.state = CorridorState.FREE
                corridor.occupying_rid = None

    def get_status_summary(self) -> Dict[int, Dict[str, Any]]:
        """스테이징 상태 요약"""
        return {
            ws_node: {
                "state": corridor.state.value,
                "occupying_rid": corridor.occupying_rid,
                "queue_size": len(corridor.queue),
                "queued_rids": [s.rid for s in corridor.queue],
            }
            for ws_node, corridor in self.corridors.items()
        }
