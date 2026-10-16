"""PostgresTms: the only code that writes business records to TMS Postgres (ADR 0002, 0008).

Writes are buffered (write-behind) and applied in one transaction per flush(). Every write is an
idempotent, monotonic upsert keyed by a deterministic id, so replaying events after a crash
converges to exactly the database an uninterrupted run produces, without moving any row
backwards (no CDC regressions):
- orders:          insert; status only moves forward in CREATED < IN_FULFILLMENT < terminal
- shipments:       insert; mutable columns update only when excluded.updated_at is newer
- trips:           insert; actual_dep / actual_arr are filled once, never overwritten
- trip_shipments,
  cod_collections: insert-only (ON CONFLICT DO NOTHING)
Within one flush a row may be written several times (e.g. a shipment's successive statuses);
those writes run as ordered "waves" with one write per key per statement, so CDC sees every
transition, in order.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, Table, case, select, text
from sqlalchemy.dialects.postgresql import insert

from vlsim.engine.clock import ms_from_datetime
from vlsim.ports import (
    CodCollectionRow,
    MasterData,
    OrderRow,
    ShipmentFacts,
    ShipmentRow,
    TripRow,
    TripStopRow,
)
from vlsim.tms import schema
from vlsim.world.build import (
    AddressRec,
    CarrierRec,
    CustomerRec,
    DepotRec,
    DriverRec,
    MerchantRec,
    VehicleRec,
)

FLUSH_ORDER = ("orders", "shipments", "trips", "trip_shipments", "cod_collections")  # FK-safe
KEYS: dict[str, tuple[str, ...]] = {
    "orders": ("order_id",),
    "shipments": ("shipment_id",),
    "trips": ("trip_id",),
    "trip_shipments": ("trip_id", "shipment_id"),
    "cod_collections": ("collection_id",),
}
ORDER_RANK = {"CREATED": 0, "IN_FULFILLMENT": 1, "DELIVERED": 2, "RETURNED": 2, "EXCEPTION": 2}
MAX_STATEMENT_ROWS = 2_000


class PostgresTms:
    def __init__(self, engine: Engine, max_buffer_rows: int = 50_000) -> None:
        self.engine = engine
        self.max_buffer_rows = max_buffer_rows
        self._buffer: dict[str, list[dict[str, Any]]] = {t: [] for t in FLUSH_ORDER}
        self._buffered = 0
        self.rows_flushed = 0

    # --- reads (master data and immutable facts only; never mutable state) ------------------
    def load_master(self) -> MasterData:
        with self.engine.connect() as conn:

            def rows(table: Table) -> list[Any]:
                pk = list(table.primary_key.columns)
                return list(conn.execute(select(table).order_by(*pk)).mappings())

            return MasterData(
                depots=tuple(DepotRec(**r) for r in rows(schema.depots)),
                carriers=tuple(CarrierRec(**r) for r in rows(schema.carriers)),
                vehicles=tuple(VehicleRec(**r) for r in rows(schema.vehicles)),
                drivers=tuple(DriverRec(**r) for r in rows(schema.drivers)),
                merchants=tuple(MerchantRec(**r) for r in rows(schema.merchants)),
                customers=tuple(CustomerRec(**r) for r in rows(schema.customers)),
                addresses=tuple(AddressRec(**r) for r in rows(schema.addresses)),
            )

    def load_shipment_facts(self, shipment_ids: Sequence[str]) -> list[ShipmentFacts]:
        if not shipment_ids:
            return []
        s, o, a = schema.shipments, schema.orders, schema.addresses
        stmt = (
            select(
                s.c.shipment_id,
                s.c.order_id,
                o.c.merchant_id,
                o.c.customer_id,
                o.c.address_id,
                s.c.origin_depot,
                s.c.dest_depot,
                o.c.service_level,
                o.c.weight_kg,
                o.c.cod_amount_vnd,
                o.c.created_at,
                s.c.promised_at,
                s.c.carrier_id,
                a.c.lat,
                a.c.lon,
                a.c.quality,
            )
            .join(o, o.c.order_id == s.c.order_id)
            .join(a, a.c.address_id == o.c.address_id)
            .where(s.c.shipment_id.in_(sorted(set(shipment_ids))))
            .order_by(s.c.shipment_id)
        )
        with self.engine.connect() as conn:
            return [
                ShipmentFacts(
                    shipment_id=r.shipment_id,
                    order_id=r.order_id,
                    merchant_id=r.merchant_id,
                    customer_id=r.customer_id,
                    address_id=r.address_id,
                    origin_depot=r.origin_depot,
                    dest_depot=r.dest_depot,
                    service_level=r.service_level,
                    weight_g=int(r.weight_kg * 1000),
                    cod_vnd=int(r.cod_amount_vnd),
                    created_ms=ms_from_datetime(r.created_at),
                    promised_ms=ms_from_datetime(r.promised_at),
                    carrier_id=r.carrier_id,
                    lat=r.lat,
                    lon=r.lon,
                    quality=r.quality,
                )
                for r in conn.execute(stmt)
            ]

    # --- buffered writes ---------------------------------------------------------------------
    def write_order(self, row: OrderRow) -> None:
        if not isinstance(row.weight_kg, Decimal):
            raise TypeError("weight_kg must be Decimal")
        self._add("orders", row)

    def write_shipment(self, row: ShipmentRow) -> None:
        self._add("shipments", row)

    def write_trip(self, row: TripRow) -> None:
        self._add("trips", row)

    def write_trip_stops(self, rows: Sequence[TripStopRow]) -> None:
        for r in rows:
            self._add("trip_shipments", r)

    def write_cod_collection(self, row: CodCollectionRow) -> None:
        self._add("cod_collections", row)

    def _add(self, table: str, row: Any) -> None:
        self._buffer[table].append(dataclasses.asdict(row))
        self._buffered += 1
        if self._buffered >= self.max_buffer_rows:
            self.flush()  # backpressure: bounded memory; flushing early is always safe

    def pending(self) -> int:
        return self._buffered

    def flush(self) -> None:
        if not self._buffered:
            return
        with self.engine.begin() as conn:
            for table in FLUSH_ORDER:
                for wave in _waves(self._buffer[table], KEYS[table]):
                    for i in range(0, len(wave), MAX_STATEMENT_ROWS):
                        _UPSERT[table](conn, wave[i : i + MAX_STATEMENT_ROWS])
        self.rows_flushed += self._buffered
        self._buffer = {t: [] for t in FLUSH_ORDER}
        self._buffered = 0

    def discard_buffer(self) -> None:
        """Test hook: lose unflushed writes, as a killed process would."""
        self._buffer = {t: [] for t in FLUSH_ORDER}
        self._buffered = 0


def _waves(rows: list[dict[str, Any]], key: tuple[str, ...]) -> list[list[dict[str, Any]]]:
    """Split rows into ordered waves holding at most one row per key (Postgres refuses to
    upsert the same row twice in one statement). The n-th write of a key lands in wave n."""
    waves: list[list[dict[str, Any]]] = []
    seen: dict[tuple[Any, ...], int] = {}
    for r in rows:
        k = tuple(r[c] for c in key)
        n = seen.get(k, 0)
        seen[k] = n + 1
        if n == len(waves):
            waves.append([])
        waves[n].append(r)
    return waves


def _rank(col: Any) -> Any:
    return case(ORDER_RANK, value=col)


def _upsert_orders(conn: Connection, rows: list[dict[str, Any]]) -> None:
    t = schema.orders
    stmt = insert(t).values(rows)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["order_id"],
            set_={"status": stmt.excluded.status},
            where=_rank(stmt.excluded.status) > _rank(t.c.status),
        )
    )


SHIPMENT_MUTABLE = ("current_status", "delivered_at", "attempt_count", "updated_at")


def _upsert_shipments(conn: Connection, rows: list[dict[str, Any]]) -> None:
    t = schema.shipments
    stmt = insert(t).values(rows)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["shipment_id"],
            set_={c: stmt.excluded[c] for c in SHIPMENT_MUTABLE},
            where=t.c.updated_at < stmt.excluded.updated_at,
        )
    )


def _upsert_trips(conn: Connection, rows: list[dict[str, Any]]) -> None:
    t = schema.trips
    stmt = insert(t).values(rows)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["trip_id"],
            set_={
                "actual_dep": text("COALESCE(trips.actual_dep, excluded.actual_dep)"),
                "actual_arr": text("COALESCE(trips.actual_arr, excluded.actual_arr)"),
            },
            where=text(
                "(trips.actual_dep IS NULL AND excluded.actual_dep IS NOT NULL) OR "
                "(trips.actual_arr IS NULL AND excluded.actual_arr IS NOT NULL)"
            ),
        )
    )


def _insert_only(table: Table) -> Any:
    def run(conn: Connection, rows: list[dict[str, Any]]) -> None:
        conn.execute(insert(table).values(rows).on_conflict_do_nothing())

    return run


_UPSERT = {
    "orders": _upsert_orders,
    "shipments": _upsert_shipments,
    "trips": _upsert_trips,
    "trip_shipments": _insert_only(schema.trip_shipments),
    "cod_collections": _insert_only(schema.cod_collections),
}
