from __future__ import annotations

import dataclasses
import re
from collections import Counter

import pytest

from vlsim.config import SimConfig, load_config
from vlsim.world.build import allocate, build_world


@pytest.fixture(scope="module")
def cfg() -> SimConfig:
    return load_config(preset="s")


def test_allocate_is_exact_and_deterministic() -> None:
    assert allocate(10, [3, 1, 1]) == [6, 2, 2]
    assert allocate(7, [1, 1, 1]) == [3, 2, 2]  # ties broken by position
    assert sum(allocate(1234, [3, 1, 1, 2, 1, 1, 3, 1, 1])) == 1234
    assert allocate(0, [1, 2]) == [0, 0]


def test_build_is_deterministic(cfg: SimConfig) -> None:
    assert build_world(cfg) == build_world(cfg)


def test_seed_changes_world(cfg: SimConfig) -> None:
    other = cfg.model_copy(update={"base": cfg.base.model_copy(update={"seed": cfg.base.seed + 1})})
    a, b = build_world(cfg), build_world(other)
    assert a.depots == b.depots  # config-only entities do not depend on the seed
    assert a.merchants != b.merchants


@pytest.mark.parametrize("preset", ["s", "m"])
def test_counts_match_preset(preset: str) -> None:
    c = load_config(preset=preset)
    w = build_world(c)
    assert len(w.depots) == 9
    assert len(w.vehicles) == c.scale.vehicle_count
    assert Counter(v.type for v in w.vehicles) == Counter(c.scale.vehicles)
    assert len(w.drivers) == c.scale.drivers
    assert len(w.merchants) == c.scale.merchants
    assert len(w.customers) == c.scale.customers
    assert len(w.addresses) >= c.scale.customers


def test_id_formats_and_uniqueness(cfg: SimConfig) -> None:
    w = build_world(cfg)
    pats = {
        "depots": (r"^DP-[A-Z]{3}$", "depot_id"),
        "carriers": (r"^CR-\d{2}$", "carrier_id"),
        "vehicles": (r"^V-\d{4,}$", "vehicle_id"),
        "drivers": (r"^D-\d{4,}$", "driver_id"),
        "merchants": (r"^M-\d{5,}$", "merchant_id"),
        "customers": (r"^C-\d{7,}$", "customer_id"),
        "addresses": (r"^A-\d{8,}$", "address_id"),
    }
    for table, (pat, col) in pats.items():
        ids = [getattr(r, col) for r in getattr(w, table)]
        assert len(ids) == len(set(ids)), table
        assert all(re.match(pat, i) for i in ids), table


def test_referential_integrity(cfg: SimConfig) -> None:
    w = build_world(cfg)
    depots = {d.depot_id for d in w.depots}
    hubs = {d.depot_id for d in w.depots if d.type == "hub"}
    customers = {c.customer_id for c in w.customers}
    assert {v.home_depot for v in w.vehicles} <= depots
    assert {v.home_depot for v in w.vehicles if v.type == "truck"} <= hubs
    assert {d.home_depot for d in w.drivers} <= depots
    assert {a.customer_id for a in w.addresses} == customers


def test_timestamps_precede_start_and_carry_offset(cfg: SimConfig) -> None:
    w = build_world(cfg)
    start = cfg.base.start_time
    stamps = [m.created_at for m in w.merchants] + [c.created_at for c in w.customers]
    for ts in stamps:
        assert ts < start
        assert ts.isoformat().endswith("+07:00")


def test_address_shape(cfg: SimConfig) -> None:
    w = build_world(cfg)
    q = Counter(a.quality for a in w.addresses)
    n = len(w.addresses)
    assert 0.75 < q["good"] / n < 0.85
    assert all(a.district is None for a in w.addresses)
    assert all(re.match(r"^Phường \d{2}$", a.ward) for a in w.addresses)
    assert all(len(str(a.lat).split(".")[-1]) <= 6 for a in w.addresses)


def test_merchant_tiers_and_cod_share(cfg: SimConfig) -> None:
    w = build_world(cfg)
    tiers = Counter(m.tier for m in w.merchants)
    assert tiers == {"enterprise": 10, "mid": 40, "small": 150}
    for m in w.merchants:
        assert m.cod_share.as_tuple().exponent == -3
        assert 0 <= m.cod_share <= 1


def test_records_are_plain_dataclasses(cfg: SimConfig) -> None:
    w = build_world(cfg)
    assert dataclasses.is_dataclass(w.vehicles[0])
