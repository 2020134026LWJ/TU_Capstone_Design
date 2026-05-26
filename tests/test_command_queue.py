"""command_queue 모듈 단위 테스트 (REFACTOR E 단계 2.1)."""
import pytest

from server.command_queue import CommandEntry, CommandQueue


def test_initial_state_is_idle():
    q = CommandQueue(rid=1)
    assert q.is_idle()
    assert q.in_flight is None
    assert len(q.pending) == 0
    assert q.peek_expected_node() is None
    assert q.peek_next() is None


def test_enqueue_then_dispatch_ack_cycle():
    q = CommandQueue(rid=1)
    entry = CommandEntry(cmd="forward", target_node=10)
    q.enqueue(entry)

    assert not q.is_idle()
    assert q.can_dispatch()
    assert q.peek_next() is entry
    assert q.peek_expected_node() == 10

    dispatched = q.dispatch()
    assert dispatched is entry
    assert q.in_flight is entry
    assert not q.can_dispatch()      # in_flight 점유 중
    assert q.peek_expected_node() == 10  # in_flight 여전히 forward

    acked = q.ack()
    assert acked is entry
    assert q.in_flight is None
    assert q.is_idle()


def test_dispatch_while_in_flight_raises():
    q = CommandQueue(rid=1)
    q.enqueue(CommandEntry(cmd="forward", target_node=10))
    q.enqueue(CommandEntry(cmd="forward", target_node=11))

    q.dispatch()  # 첫 번째 in_flight
    with pytest.raises(RuntimeError):
        q.dispatch()  # I1 위반


def test_enqueue_many_preserves_order():
    q = CommandQueue(rid=1)
    entries = [
        CommandEntry(cmd="forward", target_node=10),
        CommandEntry(cmd="turn_right", expected_heading=180),
        CommandEntry(cmd="forward", target_node=20),
    ]
    q.enqueue_many(entries)
    assert list(q.pending) == entries
    assert q.peek_next() is entries[0]


def test_peek_expected_node_in_flight_vs_pending():
    q = CommandQueue(rid=1)
    # in_flight이 turn이고 pending head가 forward
    q.enqueue(CommandEntry(cmd="turn_left", expected_heading=270))
    q.enqueue(CommandEntry(cmd="forward", target_node=15))
    q.dispatch()

    # in_flight은 turn이므로 target_node 없음 → pending head 봄
    assert q.peek_expected_node() == 15


def test_peek_expected_node_non_forward_in_flight():
    q = CommandQueue(rid=1)
    q.enqueue(CommandEntry(cmd="lift_up"))
    q.dispatch()
    assert q.peek_expected_node() is None  # lift는 forward 아님


def test_clear_pending_keeps_in_flight():
    q = CommandQueue(rid=1)
    q.enqueue(CommandEntry(cmd="forward", target_node=10))
    q.enqueue(CommandEntry(cmd="forward", target_node=11))
    q.enqueue(CommandEntry(cmd="forward", target_node=12))
    q.dispatch()  # in_flight = forward→10

    q.clear_pending()
    assert q.in_flight is not None  # AGV가 이미 받아서 실행 중
    assert q.in_flight.target_node == 10
    assert len(q.pending) == 0
    assert not q.is_idle()  # in_flight 남아있어서 idle 아님


def test_ack_when_no_in_flight_returns_none():
    q = CommandQueue(rid=1)
    assert q.ack() is None


def test_full_path_simulation():
    """경로 [9, 10, 11, 20] forward 3개 + lift_up 시뮬."""
    q = CommandQueue(rid=1)
    q.enqueue_many([
        CommandEntry(cmd="forward", target_node=10),
        CommandEntry(cmd="forward", target_node=11),
        CommandEntry(cmd="forward", target_node=20),
        CommandEntry(cmd="lift_up"),
    ])
    nodes_reached = []
    while q.can_dispatch():
        entry = q.dispatch()
        nodes_reached.append(entry.target_node)
        q.ack()
    assert nodes_reached == [10, 11, 20, None]
    assert q.is_idle()


def test_repr_does_not_crash():
    q = CommandQueue(rid=2)
    repr(q)
    q.enqueue(CommandEntry(cmd="forward", target_node=5))
    q.dispatch()
    repr(q)
