"""Deterministic seed world (master data) built from config and named RNG streams.

Pure Python + numpy: no database access here. The TMS service layer persists the result.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

import numpy as np

from vlsim.config import AddressQuality, SimConfig
from vlsim.engine.rng import stream

KM_PER_DEG_LAT = 111.32
QUALITIES: tuple[AddressQuality, ...] = ("good", "ambiguous", "bad")  # fixed draw order


@dataclass(frozen=True, slots=True)
class DepotRec:
    depot_id: str
    code: str
    name: str
    type: str
    lat: float
    lon: float
    region: str
    dock_doors: int


@dataclass(frozen=True, slots=True)
class CarrierRec:
    carrier_id: str
    name: str
    type: str
    reliability_tier: str


@dataclass(frozen=True, slots=True)
class VehicleRec:
    vehicle_id: str
    type: str
    model: str
    home_depot: str
    capacity_kg: int


@dataclass(frozen=True, slots=True)
class DriverRec:
    driver_id: str
    home_depot: str
    shift_start: time
    shift_hours_limit: int


@dataclass(frozen=True, slots=True)
class MerchantRec:
    merchant_id: str
    name: str
    tier: str
    cod_share: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerRec:
    customer_id: str
    name_masked: str
    phone_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AddressRec:
    address_id: str
    customer_id: str
    province: str
    district: str | None
    ward: str
    street_raw: str
    lat: float
    lon: float
    quality: str


@dataclass(frozen=True, slots=True)
class World:
    depots: tuple[DepotRec, ...]
    carriers: tuple[CarrierRec, ...]
    vehicles: tuple[VehicleRec, ...]
    drivers: tuple[DriverRec, ...]
    merchants: tuple[MerchantRec, ...]
    customers: tuple[CustomerRec, ...]
    addresses: tuple[AddressRec, ...]


def depot_id(code: str) -> str:
    return f"DP-{code}"


def allocate(total: int, weights: list[float]) -> list[int]:
    """Split `total` by weights with the largest-remainder method; ties go to the earlier index."""
    wsum = sum(weights)
    quotas = [total * w / wsum for w in weights]
    counts = [math.floor(q) for q in quotas]
    order = sorted(range(len(weights)), key=lambda i: (-(quotas[i] - counts[i]), i))
    for i in order[: total - sum(counts)]:
        counts[i] += 1
    return counts


def _created_at(rng: np.random.Generator, n: int, start: datetime, max_days: int) -> list[datetime]:
    secs = rng.integers(1, max_days * 86_400, size=n, endpoint=True)
    return [start - timedelta(seconds=int(s)) for s in secs]


def _build_vehicles(cfg: SimConfig) -> tuple[VehicleRec, ...]:
    rng = stream(cfg.base.seed, "world.vehicles")
    depots = cfg.network.depots
    out: list[VehicleRec] = []
    for vtype in ("truck", "van", "motorbike"):  # fixed order, independent of YAML key order
        n = cfg.scale.vehicles.get(vtype, 0)
        if n == 0:
            continue
        hubs_only = vtype == "truck" and cfg.world.trucks_at_hubs_only
        homes = [d for d in depots if d.type == "hub" or not hubs_only]
        per_depot = allocate(n, [d.demand_weight for d in homes])
        models = cfg.world.vehicle_models[vtype]
        per_model = allocate(n, [m.share for m in models])
        model_seq = [m for m, k in zip(models, per_model, strict=True) for _ in range(k)]
        model_seq = [model_seq[i] for i in rng.permutation(n)]
        home_seq = [d for d, k in zip(homes, per_depot, strict=True) for _ in range(k)]
        for home, model in zip(home_seq, model_seq, strict=True):
            vid = f"V-{len(out) + 1:04d}"
            out.append(VehicleRec(vid, vtype, model.model, depot_id(home.code), model.capacity_kg))
    return tuple(out)


def _build_drivers(cfg: SimConfig, vehicles: tuple[VehicleRec, ...]) -> tuple[DriverRec, ...]:
    codes = [depot_id(d.code) for d in cfg.network.depots]
    per_depot_vehicles = [sum(v.home_depot == c for v in vehicles) for c in codes]
    per_depot = allocate(cfg.scale.drivers, [max(k, 1) for k in per_depot_vehicles])
    starts = cfg.world.drivers.shift_starts
    out: list[DriverRec] = []
    for code, k in zip(codes, per_depot, strict=True):
        for j in range(k):
            did = f"D-{len(out) + 1:04d}"
            out.append(
                DriverRec(did, code, starts[j % len(starts)], cfg.world.drivers.shift_hours_limit)
            )
    return tuple(out)


def _build_merchants(cfg: SimConfig) -> tuple[MerchantRec, ...]:
    rng = stream(cfg.base.seed, "world.merchants")
    spec = cfg.world.merchants
    n = cfg.scale.merchants
    per_tier = allocate(n, [t.share for t in spec.tiers])
    tiers = [t for t, k in zip(spec.tiers, per_tier, strict=True) for _ in range(k)]
    tiers = [tiers[i] for i in rng.permutation(n)]
    u = rng.random(n)
    prefixes = rng.integers(0, len(spec.name_prefixes), size=n)
    created = _created_at(rng, n, cfg.base.start_time, spec.created_days_before_start_max)
    out = []
    for i in range(n):
        t = tiers[i]
        share = t.cod_share_min + float(u[i]) * (t.cod_share_max - t.cod_share_min)
        mid = f"M-{i + 1:05d}"
        out.append(
            MerchantRec(
                merchant_id=mid,
                name=f"{spec.name_prefixes[int(prefixes[i])]} {i + 1:05d}",
                tier=t.tier,
                cod_share=Decimal(f"{share:.3f}"),
                created_at=created[i],
            )
        )
    return tuple(out)


def _build_customers_and_addresses(
    cfg: SimConfig,
) -> tuple[tuple[CustomerRec, ...], tuple[AddressRec, ...]]:
    seed = cfg.base.seed
    rng_c = stream(seed, "world.customers")
    rng_a = stream(seed, "world.addresses")
    cspec, aspec = cfg.world.customers, cfg.world.addresses
    depots = cfg.network.depots
    n = cfg.scale.customers

    per_depot = allocate(n, [d.demand_weight for d in depots])
    home = [d for d, k in zip(depots, per_depot, strict=True) for _ in range(k)]
    home = [home[i] for i in rng_c.permutation(n)]
    surnames = rng_c.integers(0, len(cspec.surnames), size=n)
    initials = rng_c.integers(0, 26, size=n)
    phones = rng_c.integers(0, 100_000_000, size=n)
    created = _created_at(rng_c, n, cfg.base.start_time, cspec.created_days_before_start_max)
    n_addr = 1 + (rng_c.random(n) < cspec.second_address_prob).astype(np.int64)

    customers: list[CustomerRec] = []
    addresses: list[AddressRec] = []
    q_p = [aspec.quality_shares[q] for q in QUALITIES]
    for i in range(n):
        cid = f"C-{i + 1:07d}"
        phone = f"09{int(phones[i]):08d}"
        customers.append(
            CustomerRec(
                customer_id=cid,
                name_masked=f"{cspec.surnames[int(surnames[i])]} {chr(65 + int(initials[i]))}***",
                phone_hash=hashlib.sha256(phone.encode()).hexdigest(),
                created_at=created[i],
            )
        )
        d = home[i]
        sd_km = aspec.radius_km_sd[d.type]
        for _ in range(int(n_addr[i])):
            ward = int(rng_a.integers(1, aspec.wards_per_depot, endpoint=True))
            street_no = int(rng_a.integers(1, aspec.streets_per_ward, endpoint=True))
            house = int(rng_a.integers(1, aspec.house_number_max, endpoint=True))
            quality = QUALITIES[int(rng_a.choice(len(QUALITIES), p=q_p))]
            dy, dx = rng_a.normal(0.0, sd_km, size=2)
            lat = d.lat + float(dy) / KM_PER_DEG_LAT
            lon = d.lon + float(dx) / (KM_PER_DEG_LAT * math.cos(math.radians(d.lat)))
            street = f"Đường số {street_no}"
            addresses.append(
                AddressRec(
                    address_id=f"A-{len(addresses) + 1:08d}",
                    customer_id=cid,
                    province=d.province,
                    district=None,
                    ward=f"Phường {ward:02d}",
                    street_raw=aspec.street_templates[quality].format(house=house, street=street),
                    lat=round(lat, aspec.coord_decimals),
                    lon=round(lon, aspec.coord_decimals),
                    quality=quality,
                )
            )
    return tuple(customers), tuple(addresses)


def build_world(cfg: SimConfig) -> World:
    depots = tuple(
        DepotRec(depot_id(d.code), d.code, d.name, d.type, d.lat, d.lon, d.region, d.dock_doors)
        for d in cfg.network.depots
    )
    carriers = tuple(
        CarrierRec(c.carrier_id, c.name, c.type, c.reliability_tier) for c in cfg.world.carriers
    )
    vehicles = _build_vehicles(cfg)
    drivers = _build_drivers(cfg, vehicles)
    merchants = _build_merchants(cfg)
    customers, addresses = _build_customers_and_addresses(cfg)
    return World(depots, carriers, vehicles, drivers, merchants, customers, addresses)


def merchant_depots(cfg: SimConfig, merchant_ids: list[str]) -> dict[str, str]:
    """Pickup depot per merchant, derived (not stored): split by demand_weight, shuffled."""
    ids = sorted(merchant_ids)
    depots = cfg.network.depots
    per_depot = allocate(len(ids), [d.demand_weight for d in depots])
    homes = [depot_id(d.code) for d, k in zip(depots, per_depot, strict=True) for _ in range(k)]
    perm = stream(cfg.base.seed, "world.merchant_depots").permutation(len(ids))
    return {ids[i]: homes[int(perm[i])] for i in range(len(ids))}
