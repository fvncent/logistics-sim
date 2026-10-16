"""Order demand: non-homogeneous Poisson arrivals sampled by thinning.

lambda(t) = shipments_per_day/day x hour_of_day x day_of_week x season(local date). The thinning
bound is constant within a local day (hour curve max x that day's factors); at midnight the
sampler restarts with the next day's bound, which is valid because Poisson gaps are memoryless.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, timedelta

import numpy as np

from vlsim.config import CodValueSpec, LogNormalSpec, SimConfig
from vlsim.engine.clock import DAY_MS, HOUR_MS, local_midnight_ms, ms_to_datetime
from vlsim.models.routes import _offset_minutes
from vlsim.world.build import MerchantRec

TIER_RANK = {"enterprise": 0, "mid": 1, "small": 2}


class DemandModel:
    def __init__(self, cfg: SimConfig) -> None:
        d = cfg.demand
        self.offset_minutes = _offset_minutes(cfg.base.timezone_offset)
        self.base_per_ms = cfg.scale.shipments_per_day / DAY_MS
        self.hour = d.hour_of_day
        self.dow = d.day_of_week
        self.hour_max = max(self.hour)
        self.events = d.seasonal_events
        self._season_cache: dict[date, float] = {}

    def local_date(self, t_ms: int) -> date:
        return ms_to_datetime(t_ms, self.offset_minutes).date()

    def next_local_midnight(self, t_ms: int) -> int:
        return local_midnight_ms(t_ms, self.offset_minutes) + DAY_MS

    def season(self, day: date) -> float:
        cached = self._season_cache.get(day)
        if cached is not None:
            return cached
        mult = 1.0
        for ev in self.events:
            for anchor in self._anchors(ev.name, ev.month_day, ev.dates, ev.points, day):
                mult *= _piecewise((day - anchor).days, ev.points)
        self._season_cache[day] = mult
        return mult

    @staticmethod
    def _anchors(
        name: str,
        month_day: str | None,
        dates: Sequence[date],
        points: Sequence[tuple[int, float]],
        day: date,
    ) -> list[date]:
        """Anchor dates whose window can cover `day`: day - last_offset .. day - first_offset."""
        lo = day - timedelta(days=points[-1][0])
        hi = day - timedelta(days=points[0][0])
        if month_day is not None:
            mm, dd = (int(x) for x in month_day.split("-"))
            candidates = [date(y, mm, dd) for y in range(lo.year, hi.year + 1)]
        else:
            # One anchor per year (e.g. Tet): every year the window touches must be configured.
            missing = set(range(lo.year, hi.year + 1)) - {d.year for d in dates}
            if missing:
                raise ValueError(f"no {name} date configured for {sorted(missing)}")
            candidates = list(dates)
        return [a for a in candidates if lo <= a <= hi]

    def rate_per_ms(self, t_ms: int) -> float:
        local = ms_to_datetime(t_ms, self.offset_minutes)
        return (
            self.base_per_ms
            * self.hour[local.hour]
            * self.dow[local.weekday()]
            * self.season(local.date())
        )

    def day_bound_per_ms(self, t_ms: int) -> float:
        local = ms_to_datetime(t_ms, self.offset_minutes)
        return (
            self.base_per_ms * self.hour_max * self.dow[local.weekday()] * self.season(local.date())
        )


def _piecewise(x: int, points: Sequence[tuple[int, float]]) -> float:
    xs = [p[0] for p in points]
    if x <= xs[0] or x >= xs[-1]:
        return 1.0
    i = bisect_right(xs, x) - 1
    (x0, y0), (x1, y1) = points[i], points[i + 1]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def zipf_probabilities(
    merchants: Sequence[MerchantRec], exponent: float
) -> tuple[list[str], np.ndarray]:
    """Merchant ids ranked by (tier, id) and their choice probabilities ~ rank^-exponent."""
    ranked = sorted(merchants, key=lambda m: (TIER_RANK[m.tier], m.merchant_id))
    w = np.arange(1, len(ranked) + 1, dtype=np.float64) ** -exponent
    return [m.merchant_id for m in ranked], w / w.sum()


def draw_weight_g(rng: np.random.Generator, spec: LogNormalSpec) -> int:
    kg = spec.median * math.exp(spec.sigma * float(rng.standard_normal()))
    return round(min(max(kg, spec.min), spec.max) * 1000)


def draw_cod_vnd(rng: np.random.Generator, spec: CodValueSpec) -> int:
    v = spec.median * math.exp(spec.sigma * float(rng.standard_normal()))
    v = min(max(v, spec.min), spec.max)
    return round(v / spec.round_to) * spec.round_to


__all__ = ["HOUR_MS", "DemandModel", "draw_cod_vnd", "draw_weight_g", "zipf_probabilities"]
