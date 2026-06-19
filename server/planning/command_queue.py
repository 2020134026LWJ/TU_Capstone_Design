"""
AGV 명령 큐 (REFACTOR E 단계 2.1)

AGV 1대마다 1개의 명령 큐. 모든 cmd 발행은 이 큐를 거침 (I1 단일 발행점).
ACK 받기 전까지 다음 cmd 발행 불가 (I3 no stale state).

상세: server/REFACTOR_E.md 1.2
"""
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional


@dataclass
class CommandEntry:
    """AGV 명령 1개. publish 시 mqtt로 cmd 문자열만 전송하고,
    target_node/expected_heading은 서버 측 lifecycle 추적용."""
    cmd: str                                # "forward" / "turn_*" / "lift_*"
    target_node: Optional[int] = None       # forward: 도착 예정 노드
    expected_heading: Optional[int] = None  # turn 후 예상 heading (0/90/180/270)
    issued_at: float = 0.0                  # publish 시각 (디버깅/타임아웃용)


class CommandQueue:
    """AGV 1대의 명령 lifecycle 관리 (FIFO pending + 단일 in-flight 슬롯).

    invariant:
    - I1 (단일 발행점): 외부에서 mqtt publish는 dispatch()가 반환한 entry로만.
    - I2 (가용): is_idle() == True ⇔ 신규 task 받을 수 있음.
    - I3 (no stale): in_flight is not None일 때 robot 상태(current_node 등) 변경 금지.
    - I4 (ACK 일치): ack() 호출 시점에 in_flight cmd와 ACK 종류 일치 확인 (외부 검증).
    """

    def __init__(self, rid: int):
        self.rid = rid
        self.pending: Deque[CommandEntry] = deque()
        self.in_flight: Optional[CommandEntry] = None

    # ─── enqueue ───

    def enqueue(self, entry: CommandEntry) -> None:
        self.pending.append(entry)

    def enqueue_many(self, entries: List[CommandEntry]) -> None:
        self.pending.extend(entries)

    # ─── dispatch (in_flight으로 이동) ───

    def can_dispatch(self) -> bool:
        """발행 가능 = in_flight 없음 + pending 있음."""
        return self.in_flight is None and len(self.pending) > 0

    def peek_next(self) -> Optional[CommandEntry]:
        """pending head 미리보기 (충돌 체크용). pending 비어있으면 None."""
        return self.pending[0] if self.pending else None

    def dispatch(self) -> CommandEntry:
        """다음 cmd를 in_flight으로 옮김. 호출 전 can_dispatch() 확인 필수.

        Raises:
            IndexError: pending 비어있는데 호출
            RuntimeError: 이미 in_flight 있는데 호출 (I1 위반)
        """
        if self.in_flight is not None:
            raise RuntimeError(
                f"CommandQueue(rid={self.rid}): dispatch while in_flight={self.in_flight}"
            )
        entry = self.pending.popleft()
        self.in_flight = entry
        return entry

    # ─── ack (in_flight 완료) ───

    def ack(self) -> Optional[CommandEntry]:
        """in_flight 완료 처리. 슬롯 비우고 반환. None이면 ACK 중복/누락 의심."""
        entry = self.in_flight
        self.in_flight = None
        return entry

    # ─── 조회 ───

    def is_idle(self) -> bool:
        """완전 idle: in_flight 없음 + pending 비어있음 (수정 48 가드의 정확한 대체)."""
        return self.in_flight is None and not self.pending

    def peek_expected_node(self) -> Optional[int]:
        """forward 충돌 체크용 — in_flight 또는 pending head의 target_node.

        의미: "이 robot이 곧 진입할 노드". 다른 robot이 같은 노드 향하면 충돌 예상.
        """
        if self.in_flight is not None and self.in_flight.cmd == "forward":
            return self.in_flight.target_node
        head = self.peek_next()
        if head is not None and head.cmd == "forward":
            return head.target_node
        return None

    # ─── 재계획 (인터셉트/replan) ───

    def clear_pending(self) -> None:
        """미발행 명령 폐기. in_flight은 유지 (AGV가 이미 받아서 실행 중).

        사용처: 인터셉트 / deadlock yield / 신규 path 발행 직전.
        """
        self.pending.clear()

    # ─── 디버깅 ───

    def __repr__(self) -> str:
        in_flight_str = f"{self.in_flight.cmd}" if self.in_flight else "None"
        return (f"CommandQueue(rid={self.rid}, in_flight={in_flight_str}, "
                f"pending={len(self.pending)})")
