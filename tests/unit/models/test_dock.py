"""Dock doors as a FIFO resource: capacity, order, and queueing sanity."""

from __future__ import annotations

import heapq

import numpy as np

from vlsim.engine.rng import stream
from vlsim.models.dock import DockQueue


def test_fifo_and_capacity() -> None:
    dq = DockQueue.new(doors=2)
    started = [dq.arrive(job) for job in ("a", "b", "c", "d")]
    assert started == [True, True, False, False]
    assert dq.busy == 2 and dq.waiting == ["c", "d"]
    assert dq.release() == "c"
    assert dq.release() == "d"
    assert dq.release() is None and dq.busy == 1


def _mean_wait(utilization: float, doors: int = 4, n: int = 20_000) -> float:
    """Drive DockQueue itself with Poisson arrivals and fixed service (an M/D/c queue)."""
    rng = stream(5, f"dock.{utilization}")
    service = 10.0
    arrivals = np.cumsum(rng.exponential(service / (utilization * doors), n))
    dq = DockQueue.new(doors=doors)
    completions: list[float] = []  # heap of finish times of jobs on a door
    waits = []
    for i, t in enumerate(arrivals):
        while completions and completions[0] <= t:  # release doors that finished before t
            done = heapq.heappop(completions)
            nxt = dq.release()
            if nxt is not None:
                waits.append(done - arrivals[nxt])
                heapq.heappush(completions, done + service)
        if dq.arrive(i):
            waits.append(0.0)
            heapq.heappush(completions, t + service)
    return float(np.mean(waits))


def test_waits_rise_sharply_near_full_utilization() -> None:
    w50, w80, w95 = _mean_wait(0.5), _mean_wait(0.8), _mean_wait(0.95)
    assert w50 < w80 < w95
    assert w95 > 4 * w80 > 0  # M/D/4: ~3.9 -> ~19.5 service-time units
