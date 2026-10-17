from __future__ import annotations

import numpy as np
import pytest

from vlsim.config import load_config
from vlsim.emitters.gps import generate_pings, ping_times
from vlsim.engine.rng import stream

SPEC = load_config().emitters.gps
T0 = 1_790_787_600_000
HAN, DAD = (21.03, 105.85), (16.05, 108.20)


def _linehaul(seed: int = 1, base_seq: int = 100):  # type: ignore[no-untyped-def]
    wps = [[T0, *HAN], [T0 + 15 * 3_600_000, *DAD]]
    return generate_pings(
        vehicle_id="V-0001", vehicle_type="truck", waypoints=wps, base_seq=base_seq,
        spec=SPEC, circuity=1.3, rng=stream(seed, "gps.T-00000001"),
    )  # fmt: skip


def test_ping_times_strictly_after_start_and_regular() -> None:
    t = ping_times(T0, T0 + 65_000, 10_000)
    assert t.tolist() == [T0 + 10_000 * k for k in range(1, 7)]
    assert ping_times(T0, T0 + 9_999, 10_000).size == 0


def test_one_vectorized_batch_per_trip() -> None:
    b = _linehaul()
    n = 15 * 360
    assert b.t_ms.shape == b.lat.shape == b.lon.shape == b.speed.shape == b.fuel.shape == (n,)
    assert b.seq.tolist() == list(range(100, 100 + n))  # per-vehicle device_seq
    assert b.t_ms.dtype == np.int64


def test_track_goes_from_a_to_b_with_noise() -> None:
    b = _linehaul()
    assert b.lat[0] == pytest.approx(HAN[0], abs=0.01) and b.lat[-1] == pytest.approx(
        DAD[0], abs=0.01
    )
    assert np.all(np.diff(b.lat[::360]) < 0)  # heading south hour by hour
    assert 0 < np.std(b.lat - np.linspace(b.lat[0], b.lat[-1], b.lat.size)) < 0.001  # metres
    assert 45 < float(np.median(b.speed)) < 75  # ~820 km road in 15 h
    assert np.all(np.diff(b.fuel) <= 0) and b.fuel.min() >= SPEC.min_fuel_pct


def test_dwell_at_stop_has_near_zero_speed() -> None:
    stop = (21.05, 105.87)
    wps = [[T0, *HAN], [T0 + 600_000, *stop], [T0 + 1_200_000, *stop], [T0 + 1_800_000, *HAN]]
    b = generate_pings(vehicle_id="V-0010", vehicle_type="motorbike", waypoints=wps, base_seq=0,
                       spec=SPEC, circuity=1.3, rng=stream(1, "gps.T-2"))  # fmt: skip
    dwell = (b.t_ms > T0 + 610_000) & (b.t_ms < T0 + 1_190_000)
    assert float(np.median(b.speed[dwell])) < 5.0
    assert float(np.median(b.speed[~dwell])) > 5.0


def test_deterministic_per_trip_stream() -> None:
    a, b = _linehaul(), _linehaul()
    assert all(
        np.array_equal(getattr(a, f), getattr(b, f))
        for f in ("t_ms", "lat", "lon", "speed", "fuel", "seq")
    )
    assert not np.array_equal(_linehaul(seed=2).lat, a.lat)
