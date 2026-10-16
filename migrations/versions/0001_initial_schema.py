"""Initial TMS schema: every table in docs/SPEC.md.

Revision ID: 0001
Revises: (none)

Drafted with `alembic revision --autogenerate` from vlsim.tms.schema, then reviewed and FROZEN:
later schema changes go in new revisions; never edit this file. No server-side defaults: all
timestamps are written by the TMS service from the sim clock.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "carriers",
        sa.Column("carrier_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("reliability_tier", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "carrier_id ~ '^CR-[0-9]{2}$'", name=op.f("ck_carriers_carrier_id_format")
        ),
        sa.CheckConstraint(
            "reliability_tier IN ('high', 'medium', 'low')",
            name=op.f("ck_carriers_reliability_tier"),
        ),
        sa.CheckConstraint("type IN ('own', 'partner')", name=op.f("ck_carriers_type")),
        sa.PrimaryKeyConstraint("carrier_id", name=op.f("pk_carriers")),
        sa.UniqueConstraint("name", name=op.f("uq_carriers_name")),
    )
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("name_masked", sa.Text(), nullable=False),
        sa.Column("phone_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "customer_id ~ '^C-[0-9]{7,}$'", name=op.f("ck_customers_customer_id_format")
        ),
        sa.CheckConstraint(
            "phone_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_customers_phone_hash_format")
        ),
        sa.PrimaryKeyConstraint("customer_id", name=op.f("pk_customers")),
    )
    op.create_table(
        "depots",
        sa.Column("depot_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("lat", sa.Double(), nullable=False),
        sa.Column("lon", sa.Double(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("dock_doors", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("code ~ '^[A-Z]{3}$'", name=op.f("ck_depots_code_format")),
        sa.CheckConstraint("depot_id ~ '^DP-[A-Z]{3}$'", name=op.f("ck_depots_depot_id_format")),
        sa.CheckConstraint(
            "region IN ('north', 'central', 'south')", name=op.f("ck_depots_region")
        ),
        sa.CheckConstraint("type IN ('hub', 'depot')", name=op.f("ck_depots_type")),
        sa.CheckConstraint("dock_doors > 0", name=op.f("ck_depots_dock_doors_positive")),
        sa.CheckConstraint(
            "lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180", name=op.f("ck_depots_coords")
        ),
        sa.PrimaryKeyConstraint("depot_id", name=op.f("pk_depots")),
        sa.UniqueConstraint("code", name=op.f("uq_depots_code")),
    )
    op.create_table(
        "merchants",
        sa.Column("merchant_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("cod_share", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "merchant_id ~ '^M-[0-9]{5,}$'", name=op.f("ck_merchants_merchant_id_format")
        ),
        sa.CheckConstraint(
            "tier IN ('enterprise', 'mid', 'small')", name=op.f("ck_merchants_tier")
        ),
        sa.CheckConstraint("cod_share BETWEEN 0 AND 1", name=op.f("ck_merchants_cod_share_range")),
        sa.PrimaryKeyConstraint("merchant_id", name=op.f("pk_merchants")),
    )
    op.create_table(
        "addresses",
        sa.Column("address_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("province", sa.Text(), nullable=False),
        sa.Column("district", sa.Text(), nullable=True),
        sa.Column("ward", sa.Text(), nullable=False),
        sa.Column("street_raw", sa.Text(), nullable=False),
        sa.Column("lat", sa.Double(), nullable=False),
        sa.Column("lon", sa.Double(), nullable=False),
        sa.Column("quality", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "address_id ~ '^A-[0-9]{8,}$'", name=op.f("ck_addresses_address_id_format")
        ),
        sa.CheckConstraint(
            "quality IN ('good', 'ambiguous', 'bad')", name=op.f("ck_addresses_quality")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_addresses_customer_id_customers"),
        ),
        sa.PrimaryKeyConstraint("address_id", name=op.f("pk_addresses")),
    )
    op.create_index(op.f("ix_addresses_customer_id"), "addresses", ["customer_id"], unique=False)
    op.create_table(
        "cod_remittances",
        sa.Column("remit_id", sa.Text(), nullable=False),
        sa.Column("depot_id", sa.Text(), nullable=False),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("remitted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "remit_id ~ '^RM-[0-9]{8,}$'", name=op.f("ck_cod_remittances_remit_id_format")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'short')", name=op.f("ck_cod_remittances_status")
        ),
        sa.CheckConstraint("amount_vnd >= 0", name=op.f("ck_cod_remittances_amount_nonneg")),
        sa.ForeignKeyConstraint(
            ["depot_id"], ["depots.depot_id"], name=op.f("fk_cod_remittances_depot_id_depots")
        ),
        sa.PrimaryKeyConstraint("remit_id", name=op.f("pk_cod_remittances")),
    )
    op.create_index(
        op.f("ix_cod_remittances_depot_id"), "cod_remittances", ["depot_id"], unique=False
    )
    op.create_index(
        op.f("ix_cod_remittances_remitted_at"), "cod_remittances", ["remitted_at"], unique=False
    )
    op.create_table(
        "drivers",
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("home_depot", sa.Text(), nullable=False),
        sa.Column("shift_start", sa.Time(), nullable=False),
        sa.Column("shift_hours_limit", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("driver_id ~ '^D-[0-9]{4,}$'", name=op.f("ck_drivers_driver_id_format")),
        sa.CheckConstraint(
            "shift_hours_limit BETWEEN 1 AND 16", name=op.f("ck_drivers_shift_hours_range")
        ),
        sa.ForeignKeyConstraint(
            ["home_depot"], ["depots.depot_id"], name=op.f("fk_drivers_home_depot_depots")
        ),
        sa.PrimaryKeyConstraint("driver_id", name=op.f("pk_drivers")),
    )
    op.create_index(op.f("ix_drivers_home_depot"), "drivers", ["home_depot"], unique=False)
    op.create_table(
        "invoices",
        sa.Column("invoice_id", sa.Text(), nullable=False),
        sa.Column("carrier_id", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("total_vnd", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "invoice_id ~ '^INV-[0-9]{8,}$'", name=op.f("ck_invoices_invoice_id_format")
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'paid', 'disputed')", name=op.f("ck_invoices_status")
        ),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_invoices_period_order")),
        sa.CheckConstraint("total_vnd >= 0", name=op.f("ck_invoices_total_nonneg")),
        sa.ForeignKeyConstraint(
            ["carrier_id"], ["carriers.carrier_id"], name=op.f("fk_invoices_carrier_id_carriers")
        ),
        sa.PrimaryKeyConstraint("invoice_id", name=op.f("pk_invoices")),
    )
    op.create_index(op.f("ix_invoices_carrier_id"), "invoices", ["carrier_id"], unique=False)
    op.create_table(
        "rate_cards",
        sa.Column("rate_id", sa.Text(), nullable=False),
        sa.Column("carrier_id", sa.Text(), nullable=False),
        sa.Column("lane", sa.Text(), nullable=False),
        sa.Column("per_kg_vnd", sa.BigInteger(), nullable=False),
        sa.Column("per_km_vnd", sa.BigInteger(), nullable=False),
        sa.Column("min_charge_vnd", sa.BigInteger(), nullable=False),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("lane ~ '^[A-Z]{3}-[A-Z]{3}$'", name=op.f("ck_rate_cards_lane_format")),
        sa.CheckConstraint("rate_id ~ '^RC-[0-9]{6,}$'", name=op.f("ck_rate_cards_rate_id_format")),
        sa.CheckConstraint(
            "per_kg_vnd >= 0 AND per_km_vnd >= 0 AND min_charge_vnd >= 0",
            name=op.f("ck_rate_cards_money_nonneg"),
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name=op.f("ck_rate_cards_validity")
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"], ["carriers.carrier_id"], name=op.f("fk_rate_cards_carrier_id_carriers")
        ),
        sa.PrimaryKeyConstraint("rate_id", name=op.f("pk_rate_cards")),
    )
    op.create_index(
        op.f("ix_rate_cards_carrier_id_lane_valid_from"),
        "rate_cards",
        ["carrier_id", "lane", "valid_from"],
        unique=False,
    )
    op.create_table(
        "vehicles",
        sa.Column("vehicle_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("home_depot", sa.Text(), nullable=False),
        sa.Column("capacity_kg", sa.Integer(), nullable=False),
        sa.CheckConstraint("type IN ('motorbike', 'van', 'truck')", name=op.f("ck_vehicles_type")),
        sa.CheckConstraint(
            "vehicle_id ~ '^V-[0-9]{4,}$'", name=op.f("ck_vehicles_vehicle_id_format")
        ),
        sa.CheckConstraint("capacity_kg > 0", name=op.f("ck_vehicles_capacity_positive")),
        sa.ForeignKeyConstraint(
            ["home_depot"], ["depots.depot_id"], name=op.f("fk_vehicles_home_depot_depots")
        ),
        sa.PrimaryKeyConstraint("vehicle_id", name=op.f("pk_vehicles")),
    )
    op.create_index(op.f("ix_vehicles_home_depot"), "vehicles", ["home_depot"], unique=False)
    op.create_table(
        "orders",
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("address_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("service_level", sa.Text(), nullable=False),
        sa.Column("cod_amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint("order_id ~ '^O-[0-9]{8,}$'", name=op.f("ck_orders_order_id_format")),
        sa.CheckConstraint(
            "service_level IN ('standard', 'express')", name=op.f("ck_orders_service_level")
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'IN_FULFILLMENT', 'DELIVERED', 'RETURNED', 'EXCEPTION')",
            name=op.f("ck_orders_status"),
        ),
        sa.CheckConstraint("cod_amount_vnd >= 0", name=op.f("ck_orders_cod_amount_nonneg")),
        sa.CheckConstraint("weight_kg > 0", name=op.f("ck_orders_weight_positive")),
        sa.ForeignKeyConstraint(
            ["address_id"], ["addresses.address_id"], name=op.f("fk_orders_address_id_addresses")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.customer_id"], name=op.f("fk_orders_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.merchant_id"], name=op.f("fk_orders_merchant_id_merchants")
        ),
        sa.PrimaryKeyConstraint("order_id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_address_id"), "orders", ["address_id"], unique=False)
    op.create_index(op.f("ix_orders_created_at"), "orders", ["created_at"], unique=False)
    op.create_index(op.f("ix_orders_customer_id"), "orders", ["customer_id"], unique=False)
    op.create_index(op.f("ix_orders_merchant_id"), "orders", ["merchant_id"], unique=False)
    op.create_table(
        "trips",
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("vehicle_id", sa.Text(), nullable=False),
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("from_depot", sa.Text(), nullable=False),
        sa.Column("to_depot", sa.Text(), nullable=False),
        sa.Column("planned_dep", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("actual_dep", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("planned_arr", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("actual_arr", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('linehaul', 'lastmile')", name=op.f("ck_trips_kind")),
        sa.CheckConstraint("trip_id ~ '^T-[0-9]{8,}$'", name=op.f("ck_trips_trip_id_format")),
        sa.CheckConstraint(
            "actual_arr IS NULL OR actual_arr >= actual_dep", name=op.f("ck_trips_actual_order")
        ),
        sa.CheckConstraint(
            "actual_arr IS NULL OR actual_dep IS NOT NULL", name=op.f("ck_trips_arr_needs_dep")
        ),
        sa.CheckConstraint("planned_arr >= planned_dep", name=op.f("ck_trips_planned_order")),
        sa.ForeignKeyConstraint(
            ["driver_id"], ["drivers.driver_id"], name=op.f("fk_trips_driver_id_drivers")
        ),
        sa.ForeignKeyConstraint(
            ["from_depot"], ["depots.depot_id"], name=op.f("fk_trips_from_depot_depots")
        ),
        sa.ForeignKeyConstraint(
            ["to_depot"], ["depots.depot_id"], name=op.f("fk_trips_to_depot_depots")
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_id"], ["vehicles.vehicle_id"], name=op.f("fk_trips_vehicle_id_vehicles")
        ),
        sa.PrimaryKeyConstraint("trip_id", name=op.f("pk_trips")),
    )
    op.create_index(op.f("ix_trips_driver_id"), "trips", ["driver_id"], unique=False)
    op.create_index(op.f("ix_trips_planned_dep"), "trips", ["planned_dep"], unique=False)
    op.create_index(op.f("ix_trips_vehicle_id"), "trips", ["vehicle_id"], unique=False)
    op.create_table(
        "shipments",
        sa.Column("shipment_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("origin_depot", sa.Text(), nullable=False),
        sa.Column("dest_depot", sa.Text(), nullable=False),
        sa.Column("current_status", sa.Text(), nullable=False),
        sa.Column("promised_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.SmallInteger(), nullable=False),
        sa.Column("carrier_id", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(current_status = 'DELIVERED') = (delivered_at IS NOT NULL)",
            name=op.f("ck_shipments_delivered_at_iff"),
        ),
        sa.CheckConstraint(
            "current_status IN ('CREATED', 'PICKED_UP', 'AT_ORIGIN_DEPOT', 'LINEHAUL_DEPARTED', 'AT_HUB', 'AT_DEST_DEPOT', 'OUT_FOR_DELIVERY', 'DELIVERY_FAILED', 'DELIVERED', 'RETURNED', 'EXCEPTION')",
            name=op.f("ck_shipments_current_status"),
        ),
        sa.CheckConstraint(
            "shipment_id ~ '^S-[0-9]{8,}$'", name=op.f("ck_shipments_shipment_id_format")
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3", name=op.f("ck_shipments_attempt_count_range")
        ),
        sa.ForeignKeyConstraint(
            ["carrier_id"], ["carriers.carrier_id"], name=op.f("fk_shipments_carrier_id_carriers")
        ),
        sa.ForeignKeyConstraint(
            ["dest_depot"], ["depots.depot_id"], name=op.f("fk_shipments_dest_depot_depots")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.order_id"], name=op.f("fk_shipments_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["origin_depot"], ["depots.depot_id"], name=op.f("fk_shipments_origin_depot_depots")
        ),
        sa.PrimaryKeyConstraint("shipment_id", name=op.f("pk_shipments")),
    )
    op.create_index(op.f("ix_shipments_carrier_id"), "shipments", ["carrier_id"], unique=False)
    op.create_index(
        op.f("ix_shipments_current_status"), "shipments", ["current_status"], unique=False
    )
    op.create_index(op.f("ix_shipments_dest_depot"), "shipments", ["dest_depot"], unique=False)
    op.create_index(op.f("ix_shipments_order_id"), "shipments", ["order_id"], unique=False)
    op.create_index(op.f("ix_shipments_origin_depot"), "shipments", ["origin_depot"], unique=False)
    op.create_index(op.f("ix_shipments_updated_at"), "shipments", ["updated_at"], unique=False)
    op.create_table(
        "cod_collections",
        sa.Column("collection_id", sa.Text(), nullable=False),
        sa.Column("shipment_id", sa.Text(), nullable=False),
        sa.Column("driver_id", sa.Text(), nullable=False),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "collection_id ~ '^CC-[0-9]{8,}$'", name=op.f("ck_cod_collections_collection_id_format")
        ),
        sa.CheckConstraint("amount_vnd > 0", name=op.f("ck_cod_collections_amount_positive")),
        sa.ForeignKeyConstraint(
            ["driver_id"], ["drivers.driver_id"], name=op.f("fk_cod_collections_driver_id_drivers")
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_cod_collections_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("collection_id", name=op.f("pk_cod_collections")),
    )
    op.create_index(
        op.f("ix_cod_collections_collected_at"), "cod_collections", ["collected_at"], unique=False
    )
    op.create_index(
        op.f("ix_cod_collections_driver_id"), "cod_collections", ["driver_id"], unique=False
    )
    op.create_index(
        op.f("ix_cod_collections_shipment_id"), "cod_collections", ["shipment_id"], unique=False
    )
    op.create_table(
        "invoice_lines",
        sa.Column("invoice_id", sa.Text(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("shipment_id", sa.Text(), nullable=False),
        sa.Column("charge_type", sa.Text(), nullable=False),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "charge_type IN ('base', 'fuel_surcharge', 'cod_fee', 'return_fee', 'other')",
            name=op.f("ck_invoice_lines_charge_type"),
        ),
        sa.CheckConstraint("amount_vnd >= 0", name=op.f("ck_invoice_lines_amount_nonneg")),
        sa.CheckConstraint("line_no > 0", name=op.f("ck_invoice_lines_line_no_positive")),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.invoice_id"],
            name=op.f("fk_invoice_lines_invoice_id_invoices"),
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_invoice_lines_shipment_id_shipments"),
        ),
        sa.PrimaryKeyConstraint("invoice_id", "line_no", name=op.f("pk_invoice_lines")),
    )
    op.create_index(
        op.f("ix_invoice_lines_shipment_id"), "invoice_lines", ["shipment_id"], unique=False
    )
    op.create_table(
        "trip_shipments",
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("shipment_id", sa.Text(), nullable=False),
        sa.Column("stop_seq", sa.Integer(), nullable=False),
        sa.CheckConstraint("stop_seq >= 0", name=op.f("ck_trip_shipments_stop_seq_nonneg")),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.shipment_id"],
            name=op.f("fk_trip_shipments_shipment_id_shipments"),
        ),
        sa.ForeignKeyConstraint(
            ["trip_id"], ["trips.trip_id"], name=op.f("fk_trip_shipments_trip_id_trips")
        ),
        sa.PrimaryKeyConstraint("trip_id", "shipment_id", name=op.f("pk_trip_shipments")),
    )
    op.create_index(
        op.f("ix_trip_shipments_shipment_id"), "trip_shipments", ["shipment_id"], unique=False
    )

    # Full before-images on UPDATE/DELETE for CDC on in-place-updated tables (ADR 0006).
    op.execute("ALTER TABLE orders REPLICA IDENTITY FULL")
    op.execute("ALTER TABLE shipments REPLICA IDENTITY FULL")


def downgrade() -> None:
    op.drop_index(op.f("ix_trip_shipments_shipment_id"), table_name="trip_shipments")
    op.drop_table("trip_shipments")
    op.drop_index(op.f("ix_invoice_lines_shipment_id"), table_name="invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_index(op.f("ix_cod_collections_shipment_id"), table_name="cod_collections")
    op.drop_index(op.f("ix_cod_collections_driver_id"), table_name="cod_collections")
    op.drop_index(op.f("ix_cod_collections_collected_at"), table_name="cod_collections")
    op.drop_table("cod_collections")
    op.drop_index(op.f("ix_shipments_updated_at"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_origin_depot"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_order_id"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_dest_depot"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_current_status"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_carrier_id"), table_name="shipments")
    op.drop_table("shipments")
    op.drop_index(op.f("ix_trips_vehicle_id"), table_name="trips")
    op.drop_index(op.f("ix_trips_planned_dep"), table_name="trips")
    op.drop_index(op.f("ix_trips_driver_id"), table_name="trips")
    op.drop_table("trips")
    op.drop_index(op.f("ix_orders_merchant_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_created_at"), table_name="orders")
    op.drop_index(op.f("ix_orders_address_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_vehicles_home_depot"), table_name="vehicles")
    op.drop_table("vehicles")
    op.drop_index(op.f("ix_rate_cards_carrier_id_lane_valid_from"), table_name="rate_cards")
    op.drop_table("rate_cards")
    op.drop_index(op.f("ix_invoices_carrier_id"), table_name="invoices")
    op.drop_table("invoices")
    op.drop_index(op.f("ix_drivers_home_depot"), table_name="drivers")
    op.drop_table("drivers")
    op.drop_index(op.f("ix_cod_remittances_remitted_at"), table_name="cod_remittances")
    op.drop_index(op.f("ix_cod_remittances_depot_id"), table_name="cod_remittances")
    op.drop_table("cod_remittances")
    op.drop_index(op.f("ix_addresses_customer_id"), table_name="addresses")
    op.drop_table("addresses")
    op.drop_table("merchants")
    op.drop_table("depots")
    op.drop_table("customers")
    op.drop_table("carriers")
