from __future__ import annotations

from tests.fakes import InMemoryTms
from vlsim.config import load_config
from vlsim.engine.clock import HOUR_MS, MINUTE_MS, ms_from_datetime
from vlsim.engine.live import WallClockFlush
from vlsim.models.sim import SimTimeFlush, Simulation


class FakeWall:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_wall_clock_flush_every_interval() -> None:
    wall = FakeWall()
    p = WallClockFlush(1.0, now=wall)
    assert not p.due(0)
    wall.t += 0.6
    assert not p.due(0)
    wall.t += 0.5
    assert p.due(0)
    p.flushed(0)
    assert not p.due(10 * HOUR_MS)  # sim time is irrelevant in live mode


def test_sim_time_flush_on_grid() -> None:
    p = SimTimeFlush(15 * MINUTE_MS)
    assert p.due(0)
    p.flushed(0)
    assert not p.due(14 * MINUTE_MS)
    assert p.due(15 * MINUTE_MS)


class CountingTms(InMemoryTms):
    flush_calls = 0

    def flush(self) -> None:
        self.flush_calls += 1
        super().flush()


def test_live_policy_drives_flushes_not_sim_time() -> None:
    cfg = load_config(preset="s")
    wall = FakeWall()
    tms = CountingTms(cfg)
    sim = Simulation.fresh(cfg, tms)
    sim.flush_policy = WallClockFlush(cfg.operations.runner.live_flush_wall_seconds, now=wall)
    start = ms_from_datetime(cfg.base.start_time)
    sim.engine.run(until_ms=start + 2 * HOUR_MS)  # lots of sim time, no wall time
    assert not sim.flush_if_due() and tms.flush_calls == 0
    wall.t += 1.0
    assert sim.flush_if_due() and tms.flush_calls == 1
    assert tms.pending == []  # committed
