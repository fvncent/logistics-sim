"""Vectorized GPS: every ping of a trip is generated in one numpy batch (hard rule 9).

Pings fall strictly after the trip start, every `interval`, up to and including the arrival.
Positions are linear in time between waypoints (depot, stops, depot), plus receiver noise;
speed is road speed over each interval; fuel falls with road distance. All draws come from the
trip's own stream (seed + "gps.<trip_id>"), so a resumed run regenerates identical pings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from vlsim.config import GpsSpec

EARTH_RADIUS_KM = 6371.0088
M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True, slots=True)
class PingBatch:
    vehicle_id: str
    vehicle_type: str
    t_ms: np.ndarray  # int64
    lat: np.ndarray
    lon: np.ndarray
    speed: np.ndarray
    fuel: np.ndarray
    seq: np.ndarray  # int64 device_seq


def ping_times(start_ms: int, end_ms: int, interval_ms: int) -> np.ndarray:
    n = max(0, (end_ms - start_ms) // interval_ms)
    return start_ms + interval_ms * np.arange(1, n + 1, dtype=np.int64)


def ping_count(start_ms: int, end_ms: int, interval_ms: int) -> int:
    return max(0, (end_ms - start_ms) // interval_ms)


def _haversine_km(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    km: np.ndarray = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))
    return km


def generate_pings(
    *,
    vehicle_id: str,
    vehicle_type: str,
    waypoints: Sequence[Sequence[float]],
    base_seq: int,
    spec: GpsSpec,
    circuity: float,
    rng: np.random.Generator,
) -> PingBatch:
    wp = np.asarray(waypoints, dtype=np.float64)
    interval = spec.interval_seconds * 1000
    t = ping_times(int(wp[0, 0]), int(wp[-1, 0]), interval)
    n = t.size
    lat_true = np.interp(t, wp[:, 0], wp[:, 1])
    lon_true = np.interp(t, wp[:, 0], wp[:, 2])
    prev_lat = np.concatenate(([wp[0, 1]], lat_true[:-1]))
    prev_lon = np.concatenate(([wp[0, 2]], lon_true[:-1]))
    step_km = _haversine_km(prev_lat, prev_lon, lat_true, lon_true) * circuity

    # Fixed draw order: fuel start, speed noise, lat noise, lon noise.
    fuel0 = rng.uniform(spec.fuel_start_pct[0], spec.fuel_start_pct[1])
    speed = step_km / (interval / 3_600_000) + rng.normal(0.0, spec.speed_noise_kmh, n)
    noise_deg = spec.position_noise_m / M_PER_DEG_LAT
    lat = lat_true + rng.normal(0.0, noise_deg, n)
    lon = lon_true + rng.normal(0.0, noise_deg, n) / np.cos(np.radians(lat_true))
    rate = spec.fuel_pct_per_km[vehicle_type]  # type: ignore[index]
    fuel = np.maximum(fuel0 - np.cumsum(step_km) * rate, spec.min_fuel_pct)
    return PingBatch(
        vehicle_id=vehicle_id,
        vehicle_type=vehicle_type,
        t_ms=t,
        lat=np.round(lat, 6),
        lon=np.round(lon, 6),
        speed=np.round(np.maximum(speed, 0.0), 1),
        fuel=np.round(fuel, 1),
        seq=base_seq + np.arange(n, dtype=np.int64),
    )
