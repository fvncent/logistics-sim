"""Runtime context for business handlers, and the SimEngine that carries it.

Split of responsibilities (ADR 0008):
- `engine.state` (checkpointed) holds only operational state: queues of shipment ids with their
  mutable fields, vehicle and driver positions, dock queues, in-flight trips and id counters.
- `Context` (NOT checkpointed) holds static master data loaded from Postgres through the
  TmsService, derived lookups, and a cache of immutable shipment facts. On restore the cache is
  re-read from Postgres for exactly the shipments the checkpoint references.
- Handlers never read mutable entity state from Postgres, so replay after a crash is exact.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import numpy as np

from vlsim.config import SimConfig
from vlsim.emitters.outbox import Outbox
from vlsim.engine.core import Engine
from vlsim.models.demand import DemandModel, zipf_probabilities
from vlsim.models.routes import Routes
from vlsim.ports import MasterData, OrderRow, ShipmentFacts, ShipmentRow, TmsService
from vlsim.world.build import AddressRec, DriverRec, VehicleRec, merchant_depots

SHIPMENT_ID = re.compile(r"^S-\d{8,}$")


@dataclass
class Context:
    cfg: SimConfig
    tms: TmsService
    master: MasterData
    routes: Routes
    demand: DemandModel
    merchant_ids: list[str]
    merchant_p: np.ndarray
    merchant_depot: dict[str, str]
    merchant_cod_share: dict[str, float]
    customer_ids: list[str]
    addresses_by_customer: dict[str, list[AddressRec]]
    address_depot: dict[str, str]
    vehicles: dict[str, VehicleRec]
    drivers: dict[str, DriverRec]
    rider_bike: dict[str, str]  # rider driver_id -> motorbike vehicle_id
    riders_by_depot: dict[str, list[str]]
    crew: frozenset[str]  # line-haul drivers (everyone who is not a rider)
    facts: dict[str, ShipmentFacts] = field(default_factory=dict)
    outbox: Outbox = field(default_factory=Outbox)


def build_context(cfg: SimConfig, tms: TmsService) -> Context:
    master = tms.load_master()
    routes = Routes(cfg)
    merchant_ids, p = zipf_probabilities(master.merchants, cfg.demand.merchant_zipf_exponent)
    province_depot = {d.province: f"DP-{d.code}" for d in cfg.network.depots}
    by_customer: dict[str, list[AddressRec]] = defaultdict(list)
    for a in sorted(master.addresses, key=lambda a: a.address_id):
        by_customer[a.customer_id].append(a)

    rider_bike: dict[str, str] = {}
    riders_by_depot: dict[str, list[str]] = {}
    for dep in routes.depot_ids:
        drivers = sorted(d.driver_id for d in master.drivers if d.home_depot == dep)
        bikes = sorted(
            v.vehicle_id for v in master.vehicles if v.home_depot == dep and v.type == "motorbike"
        )
        pairs = list(zip(drivers, bikes, strict=False))  # first n drivers ride the n bikes
        rider_bike.update(pairs)
        riders_by_depot[dep] = [d for d, _ in pairs]

    return Context(
        cfg=cfg,
        tms=tms,
        master=master,
        routes=routes,
        demand=DemandModel(cfg),
        merchant_ids=merchant_ids,
        merchant_p=p,
        merchant_depot=merchant_depots(cfg, [m.merchant_id for m in master.merchants]),
        merchant_cod_share={m.merchant_id: float(m.cod_share) for m in master.merchants},
        customer_ids=sorted(c.customer_id for c in master.customers),
        addresses_by_customer=dict(by_customer),
        address_depot={a.address_id: province_depot[a.province] for a in master.addresses},
        vehicles={v.vehicle_id: v for v in master.vehicles},
        drivers={d.driver_id: d for d in master.drivers},
        rider_bike=rider_bike,
        riders_by_depot=riders_by_depot,
        crew=frozenset(d.driver_id for d in master.drivers) - set(rider_bike),
    )


class SimEngine(Engine):
    """Engine plus the non-checkpointed runtime context the business handlers need."""

    ctx: Context

    # --- writes through the TMS (timestamps always from the sim clock) ----------------------
    def write_shipment(
        self, sid: str, status: str, attempts: int, *, delivered: bool = False
    ) -> None:
        f = self.ctx.facts[sid]
        now = self.clock.datetime()
        self.ctx.tms.write_shipment(
            ShipmentRow(
                shipment_id=sid,
                order_id=f.order_id,
                origin_depot=f.origin_depot,
                dest_depot=f.dest_depot,
                current_status=status,
                promised_at=self.clock.datetime(f.promised_ms),
                delivered_at=now if delivered else None,
                attempt_count=attempts,
                carrier_id=f.carrier_id,
                updated_at=now,
            )
        )

    def write_order(self, sid: str, status: str) -> None:
        f = self.ctx.facts[sid]
        self.ctx.tms.write_order(
            OrderRow(
                order_id=f.order_id,
                merchant_id=f.merchant_id,
                customer_id=f.customer_id,
                address_id=f.address_id,
                created_at=self.clock.datetime(f.created_ms),
                service_level=f.service_level,
                cod_amount_vnd=f.cod_vnd,
                weight_kg=Decimal(f.weight_g) / 1000,
                status=status,
            )
        )

    def device(self, kind: str, who: str) -> str:
        """Device id for a depot code / depot id (scanners) or a driver id (handhelds)."""
        fmt = getattr(self.ctx.cfg.emitters.devices, kind)
        code = who.removeprefix("DP-")
        num = who.removeprefix("D-")
        return str(fmt.format(code=code, num=num))

    def emit_scan(
        self,
        sid: str,
        status: str,
        *,
        depot: str,
        device: str | None = None,
        driver: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Build the tracking.scans event (deterministic event_id) and queue it in the outbox."""
        em = self.ctx.cfg.emitters
        content = {
            "depot": depot.removeprefix("DP-"),
            "device_id": device or self.device("depot_scanner", depot),
            "driver_id": driver,
            "event_time": self.clock.isoformat(),
            "event_type": status,
            "reason_code": reason,
            "schema_v": em.schema_v,
            "shipment_id": sid,
        }
        n = self.counters.next(em.topics.scans)
        event_id = self.counters.event_id(em.topics.scans, n, content)
        self.ctx.outbox.add_scan(sid, {"event_id": str(event_id), **content})

    def next_id(self, kind: str) -> int:
        seq: dict[str, int] = self.state["seq"]
        seq[kind] = seq.get(kind, 0) + 1
        return seq[kind]


def as_sim(engine: Engine) -> SimEngine:
    return cast(SimEngine, engine)


def referenced_shipments(checkpoint: dict[str, Any]) -> list[str]:
    """Every shipment id that appears anywhere in a checkpoint (state or queued payloads)."""
    found: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, str):
            if SHIPMENT_ID.match(x):
                found.add(x)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(checkpoint["state"])
    walk(checkpoint["queue"])
    return sorted(found)
