"""PostgresTms write semantics: idempotent, monotonic, replay-safe."""

from __future__ import annotations

from dataclasses import astuple, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from tests.integration.conftest import seeded
from vlsim.config import load_config
from vlsim.ports import OrderRow, ShipmentRow, TripRow, TripStopRow
from vlsim.tms.service import PostgresTms
from vlsim.world.build import build_world

pytestmark = pytest.mark.integration
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def tms(scratch_engine: Engine) -> PostgresTms:
    return PostgresTms(seeded(scratch_engine, load_config(preset="s")))


def _order(status: str) -> OrderRow:
    return OrderRow("O-00000001", "M-00001", "C-0000001", "A-00000001", T0, "standard", 0,
                    Decimal("1.250"), status)  # fmt: skip


def _ship(status: str, minutes: int, attempts: int = 0) -> ShipmentRow:
    at = T0 + timedelta(minutes=minutes)
    delivered = at if status == "DELIVERED" else None
    return ShipmentRow("S-00000001", "O-00000001", "DP-HAN", "DP-SGN", status,
                       T0 + timedelta(days=4), delivered, attempts, "CR-01", at)  # fmt: skip


def _status(tms: PostgresTms) -> tuple[str, int, str]:
    with tms.engine.connect() as c:
        row = c.execute(text("SELECT s.current_status, s.attempt_count, o.status FROM shipments s "
                             "JOIN orders o USING (order_id)")).one()  # fmt: skip
    return row[0], row[1], row[2]


def test_master_data_round_trips_exactly(tms: PostgresTms) -> None:
    w = build_world(load_config(preset="s"))
    m = tms.load_master()
    for name in (
        "depots",
        "carriers",
        "vehicles",
        "drivers",
        "merchants",
        "customers",
        "addresses",
    ):
        want = sorted(getattr(w, name), key=lambda r: astuple(r)[0])
        assert list(getattr(m, name)) == want, name  # Postgres returns primary-key order


def test_successive_states_in_one_flush_all_apply_in_order(tms: PostgresTms) -> None:
    tms.write_order(_order("CREATED"))
    for i, st in enumerate(["CREATED", "PICKED_UP", "AT_ORIGIN_DEPOT"]):
        tms.write_shipment(_ship(st, i))
    tms.write_order(_order("IN_FULFILLMENT"))
    tms.flush()
    assert _status(tms) == ("AT_ORIGIN_DEPOT", 0, "IN_FULFILLMENT")


def test_replayed_older_writes_never_move_rows_backwards(tms: PostgresTms) -> None:
    tms.write_order(_order("CREATED"))
    tms.write_shipment(_ship("CREATED", 0))
    tms.write_order(_order("DELIVERED"))
    tms.write_shipment(_ship("DELIVERED", 500, attempts=1))
    tms.flush()
    with tms.engine.connect() as c:
        xmin_before = c.execute(text("SELECT xmin::text FROM shipments")).scalar_one()
    # Replay from an earlier checkpoint: the same writes again, oldest first.
    tms.write_order(_order("CREATED"))
    tms.write_shipment(_ship("CREATED", 0))
    tms.write_shipment(_ship("PICKED_UP", 60))
    tms.write_order(_order("IN_FULFILLMENT"))
    tms.write_shipment(_ship("DELIVERED", 500, attempts=1))
    tms.flush()
    with tms.engine.connect() as c:
        xmin_after = c.execute(text("SELECT xmin::text FROM shipments")).scalar_one()
    assert _status(tms) == ("DELIVERED", 1, "DELIVERED")
    assert xmin_before == xmin_after  # untouched: no CDC event for the replay


def test_trip_actuals_fill_once(tms: PostgresTms) -> None:
    dep, arr = T0 + timedelta(hours=1), T0 + timedelta(hours=9)
    trip = TripRow(
        "T-00000001", "V-0001", "D-0009", "DP-HAN", "DP-DAD", T0, dep, arr, None, "linehaul"
    )
    tms.write_trip(trip)
    tms.write_trip_stops([TripStopRow("T-00000001", "S-00000001", 0)])
    tms.write_order(_order("CREATED"))
    tms.write_shipment(_ship("CREATED", 0))
    tms.flush()
    tms.write_trip(replace(trip, actual_arr=arr))
    tms.flush()
    tms.write_trip(trip)  # replayed departure must not clear the arrival
    tms.write_trip_stops([TripStopRow("T-00000001", "S-00000001", 0)])
    tms.flush()
    with tms.engine.connect() as c:
        got = c.execute(text("SELECT actual_arr FROM trips")).scalar_one()
        stops = c.execute(text("SELECT count(*) FROM trip_shipments")).scalar_one()
    assert got == arr
    assert stops == 1


def test_discarded_buffer_writes_nothing(tms: PostgresTms) -> None:
    tms.write_order(_order("CREATED"))
    tms.discard_buffer()
    tms.flush()
    with tms.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM orders")).scalar_one() == 0
