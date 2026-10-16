"""Events as plain data and a deterministic priority queue."""

from __future__ import annotations

import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from vlsim.engine.canonical import plain_copy
from vlsim.engine.clock import _require_ms


@dataclass(frozen=True, slots=True)
class Event:
    """A scheduled handler call. Plain data only: no closures, so it serializes canonically."""

    time_ms: int
    seq: int
    handler: str
    payload: dict[str, Any]

    def to_data(self) -> list[Any]:
        return [self.time_ms, self.seq, self.handler, self.payload]

    @classmethod
    def from_data(cls, data: list[Any]) -> Event:
        time_ms, seq, handler, payload = data
        return cls(_require_ms(time_ms, "time_ms"), int(seq), str(handler), plain_copy(payload))


class EventQueue:
    """Min-heap ordered by (time_ms, seq). seq is a monotonic counter, so ties are FIFO and the
    heap never has to compare Event objects."""

    __slots__ = ("_heap", "_next_seq")

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, Event]] = []
        self._next_seq = 0

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def push(self, time_ms: int, handler: str, payload: dict[str, Any]) -> Event:
        _require_ms(time_ms, "time_ms")
        if not isinstance(handler, str) or not handler:
            raise TypeError("handler must be a non-empty str name")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        ev = Event(time_ms, self._next_seq, handler, plain_copy(payload))
        self._next_seq += 1
        heapq.heappush(self._heap, (ev.time_ms, ev.seq, ev))
        return ev

    def pop(self) -> Event:
        return heapq.heappop(self._heap)[2]

    def peek_time(self) -> int | None:
        return self._heap[0][0] if self._heap else None

    def snapshot(self) -> list[Event]:
        """All pending events in pop order."""
        return [item[2] for item in sorted(self._heap, key=lambda it: (it[0], it[1]))]

    @classmethod
    def restore(cls, events: Iterable[Event], next_seq: int) -> EventQueue:
        q = cls()
        for ev in events:
            if ev.seq >= next_seq:
                raise ValueError(f"event seq {ev.seq} >= next_seq {next_seq}")
            q._heap.append((ev.time_ms, ev.seq, ev))
        heapq.heapify(q._heap)
        q._next_seq = next_seq
        return q
