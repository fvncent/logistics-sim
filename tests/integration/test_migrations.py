from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import migrate
from vlsim.tms.schema import REPLICA_IDENTITY_FULL, metadata

pytestmark = pytest.mark.integration

SPEC_TABLES = {
    "depots", "merchants", "customers", "addresses", "orders", "shipments", "vehicles",
    "drivers", "trips", "trip_shipments", "carriers", "rate_cards", "cod_collections",
    "cod_remittances", "invoices", "invoice_lines",
}  # fmt: skip


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_from_empty_creates_every_spec_table(migrated_engine: Engine) -> None:
    assert _tables(migrated_engine) == SPEC_TABLES == set(metadata.tables)


def test_migration_matches_metadata(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, metadata) == []


def test_no_column_defaults_or_triggers(migrated_engine: Engine) -> None:
    """Timestamps (and everything else) are written by the TMS from the sim clock."""
    with migrated_engine.connect() as conn:
        defaults = conn.execute(
            text(
                "SELECT table_name, column_name, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_default IS NOT NULL"
            )
        ).all()
        triggers = conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).all()
    assert defaults == []
    assert triggers == []


def test_money_columns_are_bigint(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    money = [
        (t, c["name"], str(c["type"]))
        for t in SPEC_TABLES
        for c in insp.get_columns(t)
        if c["name"].endswith("_vnd")
    ]
    assert len(money) == 8  # 3 rate card, 2 COD, 2 invoice, 1 order
    assert all(typ == "BIGINT" for _, _, typ in money), money


def test_replica_identity(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows: dict[str, str] = dict(
            conn.execute(
                text("SELECT relname, relreplident FROM pg_class WHERE relname = ANY(:t)"),
                {"t": sorted(SPEC_TABLES)},
            ).all()
        )
    assert {t for t, ident in rows.items() if ident == "f"} == set(REPLICA_IDENTITY_FULL)
    assert {t for t, ident in rows.items() if ident == "d"} == SPEC_TABLES - set(
        REPLICA_IDENTITY_FULL
    )


def test_downgrade_to_empty_and_reapply(migrated_engine: Engine) -> None:
    migrate(migrated_engine, "base")
    assert _tables(migrated_engine) == set()
    with migrated_engine.connect() as conn:
        leftovers = conn.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname NOT LIKE 'alembic_version%'"
            )
        ).scalar_one()
    assert leftovers == 0  # no orphaned indexes, sequences or types
    migrate(migrated_engine, "head")
    assert _tables(migrated_engine) == SPEC_TABLES


def test_check_constraints_reject_bad_rows(migrated_engine: Engine) -> None:
    bad = [
        "INSERT INTO depots VALUES ('HAN', 'HAN', 'x', 'hub', 0, 0, 'north', 1)",  # id prefix
        "INSERT INTO depots VALUES ('DP-HAN', 'HAN', 'x', 'airport', 0, 0, 'north', 1)",  # enum
        "INSERT INTO carriers VALUES ('CR-01', 'x', 'own', 'excellent')",  # tier enum
    ]
    for sql in bad:
        with migrated_engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(text(sql))
