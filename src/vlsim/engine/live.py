"""Live-mode wall-clock helpers: the ONLY module allowed to read the wall clock (ruff TID251
whitelists this file). Wall time decides *when* outputs are flushed or how fast to pace;
it never influences *what* is simulated, so determinism is unaffected.

M4 follow-up: the wall-clock flush policy. M6 adds the pacer (speed knob) here.
"""

from __future__ import annotations

import time
from collections.abc import Callable


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
