from __future__ import annotations

import pytest

from vlsim.engine.events import Event, EventQueue


def test_pops_in_time_order() -> None:
    q = EventQueue()
    for t in (30, 10, 20):
        q.push(t, "h", {"t": t})
    assert [q.pop().time_ms for _ in range(3)] == [10, 20, 30]


def test_ties_broken_by_seq_fifo() -> None:
    q = EventQueue()
    for i in range(50):
        q.push(100, "h", {"i": i})
    q.push(99, "h", {"i": -1})
    out = [q.pop() for _ in range(51)]
    assert out[0].payload == {"i": -1}
    assert [e.payload["i"] for e in out[1:]] == list(range(50))
    assert [e.seq for e in out[1:]] == sorted(e.seq for e in out[1:])


def test_seq_is_monotonic_and_survives_pops() -> None:
    q = EventQueue()
    a = q.push(5, "h", {})
    q.pop()
    b = q.push(1, "h", {})
    assert b.seq == a.seq + 1


def test_payload_is_copied_and_canonical_plain_data() -> None:
    q = EventQueue()
    payload: dict[str, object] = {"b": [1, 2], "a": {"x": (3, 4)}}
    ev = q.push(1, "h", payload)
    payload["b"] = "mutated"
    assert ev.payload == {"a": {"x": [3, 4]}, "b": [1, 2]}  # tuple -> list, isolated copy


@pytest.mark.parametrize(
    "bad",
    [
        {"f": lambda: 1},
        {"nan": float("nan")},
        {1: "non-str key"},
        {"obj": object()},
        {"s": {1, 2}},
    ],
)
def test_non_plain_payload_rejected(bad: dict[object, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        EventQueue().push(1, "h", bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_time", [1.0, True, None])
def test_time_must_be_int_ms(bad_time: object) -> None:
    with pytest.raises(TypeError):
        EventQueue().push(bad_time, "h", {})  # type: ignore[arg-type]


def test_snapshot_restore_preserves_order_and_next_seq() -> None:
    q = EventQueue()
    for t in (7, 3, 3, 9, 1):
        q.push(t, "h", {"t": t})
    q.pop()
    snap = q.snapshot()
    assert [e.time_ms for e in snap] == [3, 3, 7, 9]
    r = EventQueue.restore(snap, q.next_seq)
    assert r.next_seq == q.next_seq
    assert [r.pop() for _ in range(4)] == [q.pop() for _ in range(4)]


def test_restore_rejects_seq_collision() -> None:
    ev = Event(1, 5, "h", {})
    with pytest.raises(ValueError, match="next_seq"):
        EventQueue.restore([ev], next_seq=5)


def test_event_data_round_trip() -> None:
    ev = Event(12, 3, "toy.deliver", {"id": "S-1"})
    assert Event.from_data(ev.to_data()) == ev
    assert ev.to_data() == [12, 3, "toy.deliver", {"id": "S-1"}]
