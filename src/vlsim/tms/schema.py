"""SQLAlchemy Core table definitions for the TMS database (owned by the TMS service layer).

Migrations under migrations/ are hand-written; tests/integration/test_migrations.py checks that
they and this metadata do not drift apart. Rules: prefixed text IDs, money as BIGINT VND,
enums as CHECK constraints, and NO server-side time defaults (the sim clock sets timestamps).
Only vlsim.tms may import this module; the engine side must not (ADR 0002).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    Double,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    Time,
)
from sqlalchemy.types import TIMESTAMP

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)

TS = TIMESTAMP(timezone=True)

SHIPMENT_STATUSES = (
    "CREATED",
    "PICKED_UP",
    "AT_ORIGIN_DEPOT",
    "LINEHAUL_DEPARTED",
    "AT_HUB",
    "AT_DEST_DEPOT",
    "OUT_FOR_DELIVERY",
    "DELIVERY_FAILED",
    "DELIVERED",
    "RETURNED",
    "EXCEPTION",
)
TERMINAL_SHIPMENT_STATUSES = ("DELIVERED", "RETURNED", "EXCEPTION")  # ADR 0005
ORDER_STATUSES = ("CREATED", "IN_FULFILLMENT", "DELIVERED", "RETURNED", "EXCEPTION")
MAX_DELIVERY_ATTEMPTS = 3


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


def _id(col: str, pattern: str) -> CheckConstraint:
    return CheckConstraint(f"{col} ~ '{pattern}'", name=f"{col}_format")


depots = Table(
    "depots",
    metadata,
    Column("depot_id", Text, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("lat", Double, nullable=False),
    Column("lon", Double, nullable=False),
    Column("region", Text, nullable=False),
    Column("dock_doors", SmallInteger, nullable=False),
    _id("depot_id", "^DP-[A-Z]{3}$"),
    CheckConstraint("code ~ '^[A-Z]{3}$'", name="code_format"),
    CheckConstraint(_in("type", ("hub", "depot")), name="type"),
    CheckConstraint(_in("region", ("north", "central", "south")), name="region"),
    CheckConstraint("dock_doors > 0", name="dock_doors_positive"),
    CheckConstraint("lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180", name="coords"),
)

carriers = Table(
    "carriers",
    metadata,
    Column("carrier_id", Text, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("type", Text, nullable=False),
    Column("reliability_tier", Text, nullable=False),
    _id("carrier_id", "^CR-[0-9]{2}$"),
    CheckConstraint(_in("type", ("own", "partner")), name="type"),
    CheckConstraint(_in("reliability_tier", ("high", "medium", "low")), name="reliability_tier"),
)

merchants = Table(
    "merchants",
    metadata,
    Column("merchant_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("tier", Text, nullable=False),
    Column("cod_share", Numeric(4, 3), nullable=False),
    Column("created_at", TS, nullable=False),
    _id("merchant_id", "^M-[0-9]{5,}$"),
    CheckConstraint(_in("tier", ("enterprise", "mid", "small")), name="tier"),
    CheckConstraint("cod_share BETWEEN 0 AND 1", name="cod_share_range"),
)

customers = Table(
    "customers",
    metadata,
    Column("customer_id", Text, primary_key=True),
    Column("name_masked", Text, nullable=False),
    Column("phone_hash", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    _id("customer_id", "^C-[0-9]{7,}$"),
    CheckConstraint("phone_hash ~ '^[0-9a-f]{64}$'", name="phone_hash_format"),
)

addresses = Table(
    "addresses",
    metadata,
    Column("address_id", Text, primary_key=True),
    Column("customer_id", Text, ForeignKey("customers.customer_id"), nullable=False),
    Column("province", Text, nullable=False),
    Column("district", Text, nullable=True),  # removed by the 2025 reform; nullable (A7)
    Column("ward", Text, nullable=False),
    Column("street_raw", Text, nullable=False),
    Column("lat", Double, nullable=False),
    Column("lon", Double, nullable=False),
    Column("quality", Text, nullable=False),
    _id("address_id", "^A-[0-9]{8,}$"),
    CheckConstraint(_in("quality", ("good", "ambiguous", "bad")), name="quality"),
    Index(None, "customer_id"),
)

orders = Table(
    "orders",
    metadata,
    Column("order_id", Text, primary_key=True),
    Column("merchant_id", Text, ForeignKey("merchants.merchant_id"), nullable=False),
    Column("customer_id", Text, ForeignKey("customers.customer_id"), nullable=False),
    Column("address_id", Text, ForeignKey("addresses.address_id"), nullable=False),
    Column("created_at", TS, nullable=False),
    Column("service_level", Text, nullable=False),
    Column("cod_amount_vnd", BigInteger, nullable=False),
    Column("weight_kg", Numeric(8, 3), nullable=False),
    Column("status", Text, nullable=False),
    _id("order_id", "^O-[0-9]{8,}$"),
    CheckConstraint(_in("service_level", ("standard", "express")), name="service_level"),
    CheckConstraint("cod_amount_vnd >= 0", name="cod_amount_nonneg"),
    CheckConstraint("weight_kg > 0", name="weight_positive"),
    CheckConstraint(_in("status", ORDER_STATUSES), name="status"),
    Index(None, "merchant_id"),
    Index(None, "customer_id"),
    Index(None, "address_id"),
    Index(None, "created_at"),
)

shipments = Table(
    "shipments",
    metadata,
    Column("shipment_id", Text, primary_key=True),
    Column("order_id", Text, ForeignKey("orders.order_id"), nullable=False),
    Column("origin_depot", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("dest_depot", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("current_status", Text, nullable=False),
    Column("promised_at", TS, nullable=False),
    Column("delivered_at", TS, nullable=True),
    Column("attempt_count", SmallInteger, nullable=False),
    Column("carrier_id", Text, ForeignKey("carriers.carrier_id"), nullable=False),
    Column("updated_at", TS, nullable=False),
    _id("shipment_id", "^S-[0-9]{8,}$"),
    CheckConstraint(_in("current_status", SHIPMENT_STATUSES), name="current_status"),
    CheckConstraint(
        f"attempt_count BETWEEN 0 AND {MAX_DELIVERY_ATTEMPTS}", name="attempt_count_range"
    ),
    CheckConstraint(
        "(current_status = 'DELIVERED') = (delivered_at IS NOT NULL)", name="delivered_at_iff"
    ),
    Index(None, "order_id"),
    Index(None, "current_status"),
    Index(None, "origin_depot"),
    Index(None, "dest_depot"),
    Index(None, "carrier_id"),
    Index(None, "updated_at"),
)

vehicles = Table(
    "vehicles",
    metadata,
    Column("vehicle_id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("home_depot", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("capacity_kg", Integer, nullable=False),
    _id("vehicle_id", "^V-[0-9]{4,}$"),
    CheckConstraint(_in("type", ("motorbike", "van", "truck")), name="type"),
    CheckConstraint("capacity_kg > 0", name="capacity_positive"),
    Index(None, "home_depot"),
)

drivers = Table(
    "drivers",
    metadata,
    Column("driver_id", Text, primary_key=True),
    Column("home_depot", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("shift_start", Time, nullable=False),  # local wall time of day (UTC+7)
    Column("shift_hours_limit", SmallInteger, nullable=False),
    _id("driver_id", "^D-[0-9]{4,}$"),
    CheckConstraint("shift_hours_limit BETWEEN 1 AND 16", name="shift_hours_range"),
    Index(None, "home_depot"),
)

trips = Table(
    "trips",
    metadata,
    Column("trip_id", Text, primary_key=True),
    Column("vehicle_id", Text, ForeignKey("vehicles.vehicle_id"), nullable=False),
    Column("driver_id", Text, ForeignKey("drivers.driver_id"), nullable=False),
    Column("from_depot", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("to_depot", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("planned_dep", TS, nullable=False),
    Column("actual_dep", TS, nullable=True),
    Column("planned_arr", TS, nullable=False),
    Column("actual_arr", TS, nullable=True),
    Column("kind", Text, nullable=False),
    _id("trip_id", "^T-[0-9]{8,}$"),
    CheckConstraint(_in("kind", ("linehaul", "lastmile")), name="kind"),
    CheckConstraint("planned_arr >= planned_dep", name="planned_order"),
    CheckConstraint("actual_arr IS NULL OR actual_dep IS NOT NULL", name="arr_needs_dep"),
    CheckConstraint("actual_arr IS NULL OR actual_arr >= actual_dep", name="actual_order"),
    Index(None, "vehicle_id"),
    Index(None, "driver_id"),
    Index(None, "planned_dep"),
)

trip_shipments = Table(
    "trip_shipments",
    metadata,
    Column("trip_id", Text, ForeignKey("trips.trip_id"), primary_key=True),
    Column("shipment_id", Text, ForeignKey("shipments.shipment_id"), primary_key=True),
    Column("stop_seq", Integer, nullable=False),
    CheckConstraint("stop_seq >= 0", name="stop_seq_nonneg"),
    Index(None, "shipment_id"),
)

rate_cards = Table(
    "rate_cards",
    metadata,
    Column("rate_id", Text, primary_key=True),
    Column("carrier_id", Text, ForeignKey("carriers.carrier_id"), nullable=False),
    Column("lane", Text, nullable=False),
    Column("per_kg_vnd", BigInteger, nullable=False),
    Column("per_km_vnd", BigInteger, nullable=False),
    Column("min_charge_vnd", BigInteger, nullable=False),
    Column("valid_from", TS, nullable=False),
    Column("valid_to", TS, nullable=True),
    _id("rate_id", "^RC-[0-9]{6,}$"),
    CheckConstraint("lane ~ '^[A-Z]{3}-[A-Z]{3}$'", name="lane_format"),
    CheckConstraint(
        "per_kg_vnd >= 0 AND per_km_vnd >= 0 AND min_charge_vnd >= 0", name="money_nonneg"
    ),
    CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="validity"),
    Index(None, "carrier_id", "lane", "valid_from"),
)

cod_collections = Table(
    "cod_collections",
    metadata,
    Column("collection_id", Text, primary_key=True),
    Column("shipment_id", Text, ForeignKey("shipments.shipment_id"), nullable=False),
    Column("driver_id", Text, ForeignKey("drivers.driver_id"), nullable=False),
    Column("amount_vnd", BigInteger, nullable=False),
    Column("collected_at", TS, nullable=False),
    _id("collection_id", "^CC-[0-9]{8,}$"),
    CheckConstraint("amount_vnd > 0", name="amount_positive"),
    Index(None, "shipment_id"),
    Index(None, "driver_id"),
    Index(None, "collected_at"),
)

cod_remittances = Table(
    "cod_remittances",
    metadata,
    Column("remit_id", Text, primary_key=True),
    Column("depot_id", Text, ForeignKey("depots.depot_id"), nullable=False),
    Column("amount_vnd", BigInteger, nullable=False),
    Column("remitted_at", TS, nullable=False),
    Column("status", Text, nullable=False),
    _id("remit_id", "^RM-[0-9]{8,}$"),
    CheckConstraint("amount_vnd >= 0", name="amount_nonneg"),
    CheckConstraint(_in("status", ("pending", "confirmed", "short")), name="status"),
    Index(None, "depot_id"),
    Index(None, "remitted_at"),
)

invoices = Table(
    "invoices",
    metadata,
    Column("invoice_id", Text, primary_key=True),
    Column("carrier_id", Text, ForeignKey("carriers.carrier_id"), nullable=False),
    Column("period_start", Date, nullable=False),
    Column("period_end", Date, nullable=False),
    Column("issued_at", TS, nullable=False),
    Column("total_vnd", BigInteger, nullable=False),
    Column("status", Text, nullable=False),
    _id("invoice_id", "^INV-[0-9]{8,}$"),
    CheckConstraint("period_end >= period_start", name="period_order"),
    CheckConstraint("total_vnd >= 0", name="total_nonneg"),
    CheckConstraint(_in("status", ("issued", "paid", "disputed")), name="status"),
    Index(None, "carrier_id"),
)

INVOICE_CHARGE_TYPES = ("base", "fuel_surcharge", "cod_fee", "return_fee", "other")

invoice_lines = Table(
    "invoice_lines",
    metadata,
    Column("invoice_id", Text, ForeignKey("invoices.invoice_id"), primary_key=True),
    Column("line_no", Integer, primary_key=True),
    Column("shipment_id", Text, ForeignKey("shipments.shipment_id"), nullable=False),
    Column("charge_type", Text, nullable=False),
    Column("amount_vnd", BigInteger, nullable=False),
    CheckConstraint("line_no > 0", name="line_no_positive"),
    CheckConstraint(_in("charge_type", INVOICE_CHARGE_TYPES), name="charge_type"),
    CheckConstraint("amount_vnd >= 0", name="amount_nonneg"),
    Index(None, "shipment_id"),
)

# Updated in place by the TMS; full before-images for CDC (ADR 0006).
REPLICA_IDENTITY_FULL = ("orders", "shipments")
