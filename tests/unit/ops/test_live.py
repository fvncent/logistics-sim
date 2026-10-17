"""Pacer and live runner, driven by an injected fake wall clock (no real sleeping)."""

from __future__ import annotations

import pytest

from vlsim.engine.live import LiveRunner, Pacer


class FakeWall:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def test_pacer_maps_sim_to_wall_by_speed() -> None:
    w = FakeWall()
    p = Pacer(speed=60, sim_start_ms=0, now=w.now, sleep=w.sleep)
    assert p.wall_target(60_000) == pytest.approx(1001.0)  # 1 sim minute = 1 s at 60x
    assert p.wait_until(60_000, max_wait_s=10) is True
    assert w.t == pytest.approx(1001.0) and p.drift_s == pytest.approx(0.0)


def test_pacer_sleeps_in_chunks_and_reports_not_yet() -> None:
    w = FakeWall()
    p = Pacer(speed=1, sim_start_ms=0, now=w.now, sleep=w.sleep)
    assert p.wait_until(10_000, max_wait_s=0.25) is False  # returns for housekeeping
    assert w.slept == [0.25]


def test_pacer_drift_when_behind_and_speed_change_reanchors() -> None:
    w = FakeWall()
    p = Pacer(speed=1000, sim_start_ms=0, now=w.now, sleep=w.sleep)
    w.t += 5.0  # work took 5 s of wall time
    assert p.wait_until(1_000_000, max_wait_s=1) is True  # target 1 s: already 4 s late
    assert p.drift_s == pytest.approx(4.0)
    p.set_speed(10, sim_now_ms=1_000_000)
    assert p.wall_target(1_010_000) == pytest.approx(w.t + 1.0)
    with pytest.raises(ValueError):
        p.set_speed(0.5, sim_now_ms=0)
    with pytest.raises(ValueError):
        p.set_speed(1001, sim_now_ms=0)


class TransientError(Exception):
    pass


class FakeTarget:
    """Events every 10 sim-seconds; the first housekeeping call fails transiently."""

    def __init__(self) -> None:
        self.now = 0
        self.advanced: list[int] = []
        self.housekeeping_calls = 0
        self.fail_once = True
        self.closed = False

    def now_ms(self) -> int:
        return self.now

    def next_stop_ms(self) -> int | None:
        return self.now + 10_000

    def advance(self, until_ms: int) -> int:
        self.now = until_ms
        self.advanced.append(until_ms)
        return 1

    def housekeeping(self) -> None:
        self.housekeeping_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise TransientError("db down")

    def close(self) -> None:
        self.closed = True


def test_live_runner_paces_retries_transient_errors_and_stops() -> None:
    w = FakeWall()
    t = FakeTarget()
    seen: list[dict[str, float]] = []
    r = LiveRunner(t, Pacer(speed=10, sim_start_ms=0, now=w.now, sleep=w.sleep),
                   stop_at_ms=60_000, transient=(TransientError,), observe=seen.append,
                   now=w.now, sleep=w.sleep)  # fmt: skip
    r.run()
    assert t.advanced == [10_000, 20_000, 30_000, 40_000, 50_000, 60_000]
    assert t.closed
    assert w.t >= 1000.0 + 6.0  # 60 sim-seconds at 10x
    assert r.transient_errors == 1
    assert seen and {"drift_s", "events_per_s", "speed"} <= set(seen[-1])


def test_live_runner_stop_flag_exits_promptly() -> None:
    w = FakeWall()
    t = FakeTarget()
    t.fail_once = False
    r = LiveRunner(
        t, Pacer(speed=1, sim_start_ms=0, now=w.now, sleep=w.sleep), now=w.now, sleep=w.sleep
    )
    orig = t.advance

    def advance_then_stop(until_ms: int) -> int:
        r.stop()  # e.g. SIGTERM arrives
        return orig(until_ms)

    t.advance = advance_then_stop  # type: ignore[method-assign]
    r.run()
    assert len(t.advanced) == 1 and t.closed
