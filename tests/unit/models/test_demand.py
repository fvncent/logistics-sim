from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from vlsim.config import SimConfig, load_config
from vlsim.engine.rng import stream
from vlsim.models.demand import DemandModel, draw_cod_vnd, draw_weight_g, zipf_probabilities
from vlsim.world.build import build_world

START = 1_790_787_600_000  # Thu 2026-10-01 00:00 +07:00
DAY = 86_400_000
HOUR = 3_600_000


@pytest.fixture(scope="module")
def cfg() -> SimConfig:
    return load_config(preset="s")


@pytest.fixture(scope="module")
def dm(cfg: SimConfig) -> DemandModel:
    return DemandModel(cfg)


def test_curves_are_normalized(cfg: SimConfig) -> None:
    assert sum(cfg.demand.hour_of_day) / 24 == pytest.approx(1.0)
    assert sum(cfg.demand.day_of_week) / 7 == pytest.approx(1.0)


def test_season_shape(dm: DemandModel) -> None:
    assert dm.season(date(2026, 10, 1)) == 1.0
    assert dm.season(date(2026, 11, 11)) == pytest.approx(2.5)
    assert 1.0 < dm.season(date(2026, 11, 8)) < 1.4  # ramp
    assert dm.season(date(2026, 12, 12)) == pytest.approx(2.0)
    assert dm.season(date(2027, 2, 6)) == pytest.approx(0.2)  # Tet day 1: trough
    assert dm.season(date(2027, 1, 30)) > 1.4  # pre-Tet surge


def test_season_refuses_year_without_tet_date(dm: DemandModel) -> None:
    with pytest.raises(ValueError, match="tet"):
        dm.season(date(2035, 3, 1))


def test_rate_never_exceeds_day_bound(dm: DemandModel) -> None:
    for h in range(0, 24 * 60):
        t = START + h * HOUR
        assert dm.rate_per_ms(t) <= dm.day_bound_per_ms(t) * (1 + 1e-12)


def test_expected_daily_volume_on_ordinary_week(cfg: SimConfig, dm: DemandModel) -> None:
    # Integrate the rate over a plain week (Oct 2026, no events): mean must equal the preset.
    week = sum(dm.rate_per_ms(START + m * 60_000) * 60_000 for m in range(7 * 24 * 60))
    assert week / 7 == pytest.approx(cfg.scale.shipments_per_day, rel=1e-9)


def test_thinning_sample_matches_rate(dm: DemandModel) -> None:
    """Simulate the thinning sampler alone for 28 days and compare with the integral."""
    rng_gap, rng_acc = stream(1, "t.gap"), stream(1, "t.acc")
    t, end, accepted = START, START + 28 * DAY, 0
    hourly = np.zeros(24)
    while t < end:
        bound = dm.day_bound_per_ms(t)
        midnight = dm.next_local_midnight(t)
        t_next = t + max(1, int(np.ceil(rng_gap.exponential(1.0 / bound))))
        if t_next >= midnight:
            t = midnight
            continue
        t = t_next
        if rng_acc.random() * bound < dm.rate_per_ms(t):
            accepted += 1
            hourly[((t + 7 * HOUR) // HOUR) % 24] += 1
    assert accepted / 28 == pytest.approx(1200, rel=0.03)
    assert hourly[20] > 3 * hourly[3]  # evening peak vs night trough


def test_merchants_are_heavy_tailed(cfg: SimConfig) -> None:
    for preset in ("s", "m"):
        merchants = build_world(load_config(preset=preset)).merchants
        ids, p = zipf_probabilities(merchants, cfg.demand.merchant_zipf_exponent)
        assert sorted(ids) == sorted(m.merchant_id for m in merchants)
        top20 = np.sort(p)[::-1][: len(p) // 5].sum()
        assert 0.75 < top20 < 0.88, (preset, top20)
        tiers = {m.merchant_id: m.tier for m in merchants}
        assert tiers[ids[0]] == "enterprise"  # largest shipper is an enterprise merchant


def test_weights_and_cod_are_integers_in_range(cfg: SimConfig) -> None:
    rng = stream(3, "x")
    w = [draw_weight_g(rng, cfg.demand.weight_kg) for _ in range(20_000)]
    assert all(isinstance(x, int) for x in w)
    assert min(w) >= 50 and max(w) <= 30_000
    assert np.median(w) == pytest.approx(800, rel=0.05)  # log-normal median
    cod = [draw_cod_vnd(rng, cfg.demand.cod_value_vnd) for _ in range(5_000)]
    assert all(isinstance(c, int) and c % 1000 == 0 and 10_000 <= c <= 20_000_000 for c in cod)
