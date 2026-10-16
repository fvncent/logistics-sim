"""In-memory TmsService with the same write semantics as the Postgres implementation.

Writes go to a pending buffer and become durable on flush(); crash() discards the buffer, like
a killed process. Every applied shipment write is validated against the state machine and the
"one write per shipment per millisecond" rule, which the Postgres version cannot check cheaply.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from vlsim.config import SimConfig
from vlsim.engine.clock import ms_from_datetime
from vlsim.models.statemachine import check_transition
from vlsim.ports import (
    CodCollectionRow,
    MasterData,
    OrderRow,
    ShipmentFacts,
    ShipmentRow,
    TripRow,
    TripStopRow,
)
from vlsim.world.build import build_world

ORDER_RANK = {"CREATED": 0, "IN_FULFILLMENT": 1, "DELIVERED": 2, "RETURNED": 2, "EXCEPTION": 2}


class InMemoryTms:
    def __init__(self, cfg: SimConfig) -> None:
        w = build_world(cfg)

        def pk_order(rows: tuple[Any, ...]) -> tuple[Any, ...]:
            return tuple(sorted(rows, key=lambda r: dataclasses.astuple(r)[0]))

        # Same row order as PostgresTms.load_master (primary key).
        self.master = MasterData(
            *(pk_order(getattr(w, f.name)) for f in dataclasses.fields(MasterData))
        )
        self.addresses = {a.address_id: a for a in w.addresses}
        self.tables: dict[str, dict[Any, Any]] = {
            "orders": {},
            "shipments": {},
            "trips": {},
            "trip_shipments": {},
            "cod_collections": {},
        }
        self.pending: list[tuple[str, Any]] = []
        self.history: list[tuple[str, str, int]] = []  # applied (shipment_id, status, ms)
        self.flushes = 0

    # --- TmsService -------------------------------------------------------------------------
    def load_master(self) -> MasterData:
        return self.master

    def load_shipment_facts(self, shipment_ids: Sequence[str]) -> list[ShipmentFacts]:
        out = []
        for sid in sorted(set(shipment_ids)):
            s = self.tables["shipments"][sid]
            o = self.tables["orders"][s.order_id]
            a = self.addresses[o.address_id]
            out.append(
                ShipmentFacts(
                    sid,
                    o.order_id,
                    o.merchant_id,
                    o.customer_id,
                    o.address_id,
                    s.origin_depot,
                    s.dest_depot,
                    o.service_level,
                    int(o.weight_kg * 1000),
                    o.cod_amount_vnd,
                    ms_from_datetime(o.created_at),
                    ms_from_datetime(s.promised_at),
                    s.carrier_id,
                    a.lat,
                    a.lon,
                    a.quality,
                )
            )
        return out

    def write_order(self, row: OrderRow) -> None:
        self.pending.append(("orders", row))

    def write_shipment(self, row: ShipmentRow) -> None:
        self.pending.append(("shipments", row))

    def write_trip(self, row: TripRow) -> None:
        self.pending.append(("trips", row))

    def write_trip_stops(self, rows: Sequence[TripStopRow]) -> None:
        self.pending.extend(("trip_shipments", r) for r in rows)

    def write_cod_collection(self, row: CodCollectionRow) -> None:
        self.pending.append(("cod_collections", row))

    def flush(self) -> None:
        for table, row in self.pending:
            getattr(self, f"_apply_{table}")(row)
        self.pending = []
        self.flushes += 1

    # --- test helpers -----------------------------------------------------------------------
    def crash(self) -> None:
        self.pending = []

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        return {
            t: [dataclasses.astuple(rows[k]) for k in sorted(rows)]
            for t, rows in sorted(self.tables.items())
        }

    def clone(self) -> InMemoryTms:
        return copy.deepcopy(self)

    # --- write semantics mirrored from vlsim.tms.service --------------------------------------
    def _apply_orders(self, row: OrderRow) -> None:
        cur = self.tables["orders"].get(row.order_id)
        assert isinstance(row.weight_kg, Decimal)
        if cur is None:
            self.tables["orders"][row.order_id] = row
        elif ORDER_RANK[row.status] > ORDER_RANK[cur.status]:
            self.tables["orders"][row.order_id] = dataclasses.replace(cur, status=row.status)

    def _apply_shipments(self, row: ShipmentRow) -> None:
        cur = self.tables["shipments"].get(row.shipment_id)
        ms = ms_from_datetime(row.updated_at)
        if cur is not None:
            cur_ms = ms_from_datetime(cur.updated_at)
            if ms < cur_ms or (ms == cur_ms and row == cur):
                return  # replay of an older (or identical) state: skipped, like Postgres
            if ms == cur_ms:
                raise AssertionError(f"two different writes for {row.shipment_id} at {ms}")
        check_transition(None if cur is None else cur.current_status, row.current_status)
        if cur is not None and row.attempt_count < cur.attempt_count:
            raise AssertionError(f"attempt_count went down on {row.shipment_id}")
        self.tables["shipments"][row.shipment_id] = row
        self.history.append((row.shipment_id, row.current_status, ms))

    def _apply_trips(self, row: TripRow) -> None:
        cur = self.tables["trips"].get(row.trip_id)
        if cur is None:
            self.tables["trips"][row.trip_id] = row
        else:
            self.tables["trips"][row.trip_id] = dataclasses.replace(
                cur,
                actual_dep=cur.actual_dep or row.actual_dep,
                actual_arr=cur.actual_arr or row.actual_arr,
            )

    def _apply_trip_shipments(self, row: TripStopRow) -> None:
        self.tables["trip_shipments"].setdefault((row.trip_id, row.shipment_id), row)

    def _apply_cod_collections(self, row: CodCollectionRow) -> None:
        self.tables["cod_collections"].setdefault(row.collection_id, row)
