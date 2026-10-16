"""The simulator's interface to the TMS service (ADR 0002), plus the plain rows it exchanges.

Lives outside vlsim.tms so the engine side can depend on the contract without importing the
database layer. Every timestamp in a row comes from the sim clock; the TMS never stamps time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from vlsim.world.build import (
    AddressRec,
    CarrierRec,
    CustomerRec,
    DepotRec,
    DriverRec,
    MerchantRec,
    VehicleRec,
)


@dataclass(frozen=True, slots=True)
class MasterData:
    """Seeded entities as stored in Postgres (read once at startup; they do not change)."""

    depots: tuple[DepotRec, ...]
    carriers: tuple[CarrierRec, ...]
    vehicles: tuple[VehicleRec, ...]
    drivers: tuple[DriverRec, ...]
    merchants: tuple[MerchantRec, ...]
    customers: tuple[CustomerRec, ...]
    addresses: tuple[AddressRec, ...]


@dataclass(frozen=True, slots=True)
class ShipmentFacts:
    """Immutable attributes of a shipment and its order, in exact integer units."""

    shipment_id: str
    order_id: str
    merchant_id: str
    customer_id: str
    address_id: str
    origin_depot: str
    dest_depot: str
    service_level: str
    weight_g: int
    cod_vnd: int
    created_ms: int
    promised_ms: int
    carrier_id: str
    lat: float
    lon: float
    quality: str


@dataclass(frozen=True, slots=True)
class OrderRow:
    order_id: str
    merchant_id: str
    customer_id: str
    address_id: str
    created_at: datetime
    service_level: str
    cod_amount_vnd: int
    weight_kg: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class ShipmentRow:
    shipment_id: str
    order_id: str
    origin_depot: str
    dest_depot: str
    current_status: str
    promised_at: datetime
    delivered_at: datetime | None
    attempt_count: int
    carrier_id: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TripRow:
    trip_id: str
    vehicle_id: str
    driver_id: str
    from_depot: str
    to_depot: str
    planned_dep: datetime
    actual_dep: datetime | None
    planned_arr: datetime
    actual_arr: datetime | None
    kind: str


@dataclass(frozen=True, slots=True)
class TripStopRow:
    trip_id: str
    shipment_id: str
    stop_seq: int


@dataclass(frozen=True, slots=True)
class CodCollectionRow:
    collection_id: str
    shipment_id: str
    driver_id: str
    amount_vnd: int
    collected_at: datetime


class TmsService(Protocol):
    """Business verbs the simulator uses. Writes are idempotent, monotonic upserts keyed by
    deterministic IDs, so replaying after a crash converges to the same database (ADR 0008).
    Writes may be buffered; `flush` makes everything written so far durable."""

    def load_master(self) -> MasterData: ...

    def load_shipment_facts(self, shipment_ids: Sequence[str]) -> list[ShipmentFacts]: ...

    def write_order(self, row: OrderRow) -> None: ...

    def write_shipment(self, row: ShipmentRow) -> None: ...

    def write_trip(self, row: TripRow) -> None: ...

    def write_trip_stops(self, rows: Sequence[TripStopRow]) -> None: ...

    def write_cod_collection(self, row: CodCollectionRow) -> None: ...

    def flush(self) -> None: ...
