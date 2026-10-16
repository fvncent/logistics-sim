"""Persist the seed world (master data) through the TMS layer.

Idempotent and convergent: every row is upserted by primary key, and an existing row is only
rewritten when a value differs (ON CONFLICT ... DO UPDATE ... WHERE row IS DISTINCT FROM). A
second run with the same seed and config therefore changes nothing, not even row versions, so
it emits no CDC noise. All timestamps come from the world builder (sim clock), never the DB.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from sqlalchemy import Connection, Table, tuple_
from sqlalchemy.dialects.postgresql import insert

from vlsim.tms import schema
from vlsim.world.build import World

BATCH_ROWS = 2_000

# FK-safe order: parents before children.
SEED_TABLES: tuple[tuple[str, Table], ...] = (
    ("depots", schema.depots),
    ("carriers", schema.carriers),
    ("vehicles", schema.vehicles),
    ("drivers", schema.drivers),
    ("merchants", schema.merchants),
    ("customers", schema.customers),
    ("addresses", schema.addresses),
)


def _upsert(conn: Connection, table: Table, rows: Sequence[object]) -> int:
    """Upsert rows; returns how many rows were inserted or actually changed."""
    if not rows:
        return 0
    pk = [c.name for c in table.primary_key.columns]
    cols = [c.name for c in table.columns if c.name not in pk]
    written = 0
    for start in range(0, len(rows), BATCH_ROWS):
        batch = [dataclasses.asdict(r) for r in rows[start : start + BATCH_ROWS]]  # type: ignore[call-overload]
        stmt = insert(table).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=pk,
            set_={c: stmt.excluded[c] for c in cols},
            where=tuple_(*(table.c[c] for c in cols)).is_distinct_from(
                tuple_(*(stmt.excluded[c] for c in cols))
            ),
        )
        # RETURNING lists only rows actually inserted or updated (skipped no-ops are omitted).
        written += len(conn.execute(stmt.returning(*table.primary_key.columns)).all())
    return written


def seed_world(conn: Connection, world: World) -> dict[str, int]:
    """Write the world in one transaction owned by the caller. Returns rows written per table."""
    return {name: _upsert(conn, table, getattr(world, name)) for name, table in SEED_TABLES}
