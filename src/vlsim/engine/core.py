"""Discrete-event engine: pop the earliest event, advance the sim clock, run its handler."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Self

import numpy as np

from vlsim.engine.canonical import canonical_json, plain_copy
from vlsim.engine.clock import SimClock, _require_ms
from vlsim.engine.events import Event, EventQueue
from vlsim.engine.ids import EmissionCounters
from vlsim.engine.rng import RngStreams

CHECKPOINT_FORMAT = 1

Handler = Callable[["Engine", Event], None]


class CheckpointError(Exception):
    """A checkpoint is corrupt or does not belong to this configuration or code."""


class HandlerRegistry:
    """Name -> module-level function. Events store only the name, so they serialize."""

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, name: str, fn: Handler) -> None:
        if name in self._handlers:
            raise ValueError(f"handler {name!r} already registered")
        qualname = getattr(fn, "__qualname__", "")
        if getattr(fn, "__closure__", None) or "<lambda>" in qualname or "<locals>" in qualname:
            raise TypeError(f"handler {name!r} must be a module-level function, not {qualname}")
        self._handlers[name] = fn

    def get(self, name: str) -> Handler:
        try:
            return self._handlers[name]
        except KeyError:
            raise KeyError(f"no handler registered as {name!r}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


class Engine:
    """Owns the clock, queue, RNG streams, emission counters and world state.

    `state` is JSON plain data owned by handlers; it is checkpointed canonically.
    """

    def __init__(
        self,
        *,
        seed: int,
        start_ms: int,
        offset_minutes: int,
        registry: HandlerRegistry,
        fingerprint: str = "",
    ) -> None:
        self.seed = seed
        self.fingerprint = fingerprint
        self.registry = registry
        self.clock = SimClock(start_ms, offset_minutes)
        self.queue = EventQueue()
        self.streams = RngStreams(seed)
        self.counters = EmissionCounters()
        self.state: dict[str, Any] = {}
        self.events_processed = 0

    # --- API for handlers -------------------------------------------------------------------
    def rng(self, name: str) -> np.random.Generator:
        return self.streams.get(name)

    def schedule(self, at_ms: int, handler: str, payload: dict[str, Any]) -> Event:
        self.registry.get(handler)  # fail fast on typos, not hours later when it fires
        if _require_ms(at_ms, "at_ms") < self.clock.now_ms:
            raise ValueError(f"cannot schedule {handler!r} in the past ({at_ms})")
        return self.queue.push(at_ms, handler, payload)

    def schedule_in(self, delay_ms: int, handler: str, payload: dict[str, Any]) -> Event:
        if _require_ms(delay_ms, "delay_ms") < 0:
            raise ValueError("delay_ms must be >= 0")
        return self.schedule(self.clock.now_ms + delay_ms, handler, payload)

    # --- loop -------------------------------------------------------------------------------
    def run(self, until_ms: int | None = None, max_events: int | None = None) -> int:
        """Process events with time <= until_ms (and at most max_events). Returns the count.

        When stopped by the time bound, the clock is advanced to until_ms so a checkpoint taken
        afterwards is at a well-defined instant.
        """
        if until_ms is not None and _require_ms(until_ms, "until_ms") < self.clock.now_ms:
            raise ValueError("until_ms is in the past")
        processed = 0
        while max_events is None or processed < max_events:
            t = self.queue.peek_time()
            if t is None or (until_ms is not None and t > until_ms):
                if until_ms is not None:
                    self.clock.advance_to(until_ms)
                break
            ev = self.queue.pop()
            self.clock.advance_to(ev.time_ms)
            self.registry.get(ev.handler)(self, ev)
            self.events_processed += 1
            processed += 1
        return processed

    # --- checkpoint -------------------------------------------------------------------------
    def to_checkpoint(self) -> dict[str, Any]:
        data = {
            "format": CHECKPOINT_FORMAT,
            "fingerprint": self.fingerprint,
            "seed": self.seed,
            "clock_ms": self.clock.now_ms,
            "offset_minutes": self.clock.offset_minutes,
            "next_seq": self.queue.next_seq,
            "events_processed": self.events_processed,
            "queue": [ev.to_data() for ev in self.queue.snapshot()],
            "rng": self.streams.state(),
            "counters": self.counters.data(),
            "state": plain_copy(self.state),
        }
        result: dict[str, Any] = json.loads(canonical_json(data))
        return result

    @classmethod
    def from_checkpoint(
        cls, data: dict[str, Any], registry: HandlerRegistry, *, fingerprint: str
    ) -> Self:
        if data.get("format") != CHECKPOINT_FORMAT:
            raise CheckpointError(f"unsupported checkpoint format {data.get('format')!r}")
        if data["fingerprint"] != fingerprint:
            raise CheckpointError("config fingerprint differs from the checkpoint's")
        events = [Event.from_data(e) for e in data["queue"]]
        unknown = sorted({e.handler for e in events} - set(registry.names()))
        if unknown:
            raise CheckpointError(f"checkpoint references unknown handler(s): {unknown}")
        eng = cls(
            seed=data["seed"],
            start_ms=data["clock_ms"],
            offset_minutes=data["offset_minutes"],
            registry=registry,
            fingerprint=fingerprint,
        )
        eng.queue = EventQueue.restore(events, data["next_seq"])
        eng.streams = RngStreams.restore(data["seed"], data["rng"])
        eng.counters = EmissionCounters.restore(data["counters"])
        eng.state = plain_copy(data["state"])
        eng.events_processed = data["events_processed"]
        return eng
