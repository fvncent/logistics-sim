"""Live mode: the ONLY module allowed to read or sleep on the wall clock (ruff TID251
whitelists this file). Wall time decides *when* things happen (pacing, flushes, retries,
metrics); it never influences *what* is simulated, so output stays deterministic.

- Pacer maps sim time to wall time: wall = anchor_wall + (sim - anchor_sim) / 1000 / speed,
  with a speed knob from 1x to 1000x; it reports pacing drift (how far behind wall time is).
- WallClockFlush flushes outputs about every second of wall time (ADR 0009).
- LiveRunner drives any LiveTarget (vlsim.models.sim.Simulation) under a Pacer, sleeps in
  short chunks so a stop request (SIGTERM) is honoured quickly, retries transient errors
  (e.g. Postgres unreachable) with backoff instead of crashing, and reports wall-derived stats.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

import structlog

log = structlog.get_logger()

MIN_SPEED, MAX_SPEED = 1.0, 1000.0


class WallClockFlush:
    """Flush outputs every `interval_s` seconds of wall time (live mode, ADR 0009)."""

    def __init__(self, interval_s: float, now: Callable[[], float] = time.monotonic) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self.interval_s = interval_s
        self._now = now
        self._last = now()

    def due(self, sim_now_ms: int) -> bool:  # same signature as SimTimeFlush
        return self._now() - self._last >= self.interval_s

    def flushed(self, sim_now_ms: int) -> None:
        self._last = self._now()

    def wall_since_flush(self) -> float:
        return self._now() - self._last


class Pacer:
    def __init__(
        self,
        speed: float,
        sim_start_ms: int,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._now, self._sleep = now, sleep
        self.drift_s = 0.0
        self.speed = self._check(speed)
        self._anchor_sim = sim_start_ms
        self._anchor_wall = now()

    @staticmethod
    def _check(speed: float) -> float:
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be between {MIN_SPEED:g}x and {MAX_SPEED:g}x")
        return float(speed)

    def set_speed(self, speed: float, sim_now_ms: int) -> None:
        """Change speed from now on (re-anchors at the current sim instant)."""
        self.speed = self._check(speed)
        self._anchor_sim = sim_now_ms
        self._anchor_wall = self._now()

    def wall_target(self, sim_ms: int) -> float:
        return self._anchor_wall + (sim_ms - self._anchor_sim) / 1000.0 / self.speed

    def wait_until(self, sim_ms: int, max_wait_s: float) -> bool:
        """Sleep towards the wall instant of `sim_ms`, at most `max_wait_s`. True when reached
        (drift_s then says how late we are); False when the caller should do housekeeping
        and call again."""
        remaining = self.wall_target(sim_ms) - self._now()
        if remaining <= 0:
            self.drift_s = -remaining
            return True
        if remaining > max_wait_s:
            self._sleep(max_wait_s)
            return False
        self._sleep(remaining)
        self.drift_s = max(0.0, self._now() - self.wall_target(sim_ms))
        return True


class LiveTarget(Protocol):
    def now_ms(self) -> int: ...

    def next_stop_ms(self) -> int | None: ...

    def advance(self, until_ms: int) -> int: ...

    def housekeeping(self) -> None: ...

    def close(self) -> None: ...


class LiveRunner:
    def __init__(
        self,
        target: LiveTarget,
        pacer: Pacer,
        *,
        stop_at_ms: int | None = None,
        transient: tuple[type[BaseException], ...] = (),
        observe: Callable[[dict[str, float]], None] | None = None,
        tick_s: float = 0.25,
        idle_step_ms: int = 60_000,
        max_backoff_s: float = 30.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.target, self.pacer = target, pacer
        self.stop_at_ms = stop_at_ms
        self.transient = transient
        self.observe = observe
        self.tick_s = tick_s
        self.idle_step_ms = idle_step_ms
        self.max_backoff_s = max_backoff_s
        self._now, self._sleep = now, sleep
        self._stop = False
        self.transient_errors = 0
        self.events = 0
        self._rate_wall, self._rate_events, self.events_per_s = now(), 0, 0.0

    def stop(self) -> None:
        """Request a graceful stop (safe to call from a signal handler)."""
        self._stop = True

    def run(self) -> None:
        while not self._stop:
            now = self.target.now_ms()
            if self.stop_at_ms is not None and now >= self.stop_at_ms:
                break
            horizon = self.target.next_stop_ms() or now + self.idle_step_ms
            if self.stop_at_ms is not None:
                horizon = min(horizon, self.stop_at_ms)
            if self.pacer.wait_until(horizon, self.tick_s):
                self.events += self.target.advance(horizon)
            self._retrying(self.target.housekeeping)
            self._observe()
        log.info("live_stopping", sim_ms=self.target.now_ms(), events=self.events)
        self._retrying(self.target.close)

    def _retrying(self, fn: Callable[[], None]) -> None:
        backoff = 1.0
        while True:
            try:
                fn()
                return
            except self.transient as exc:
                self.transient_errors += 1
                log.warning("transient_error_retrying", error=str(exc)[:200], backoff_s=backoff)
                self._observe(failing=True)
                self._sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)

    def _observe(self, failing: bool = False) -> None:
        wall = self._now()
        if wall - self._rate_wall >= 1.0:
            self.events_per_s = (self.events - self._rate_events) / (wall - self._rate_wall)
            self._rate_wall, self._rate_events = wall, self.events
        if self.observe is not None:
            self.observe(
                {
                    "drift_s": self.pacer.drift_s,
                    "events_per_s": self.events_per_s,
                    "speed": self.pacer.speed,
                    "wall_s": wall,
                    "failing": 1.0 if failing else 0.0,
                }
            )
