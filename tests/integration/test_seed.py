from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import Engine, text

from vlsim.config import load_config
from vlsim.tms.seed import SEED_TABLES, seed_world
from vlsim.world.build import build_world

pytestmark = pytest.mark.integration


def _snapshot(engine: Engine) -> dict[str, tuple[int, str, str]]:
    """Per table: row count, hash of full ordered contents, hash of row versions (xmin)."""
    out = {}
    with engine.connect() as conn:
        for name, table in SEED_TABLES:
            pk = ", ".join(c.name for c in table.primary_key.columns)
            rows = conn.execute(text(f"SELECT t::text, t.xmin::text FROM {name} t ORDER BY {pk}"))
            data, versions = hashlib.sha256(), hashlib.sha256()
            n = 0
            for row_text, xmin in rows:
                data.update(row_text.encode())
                versions.update(xmin.encode())
                n += 1
            out[name] = (n, data.hexdigest(), versions.hexdigest())
    return out


def test_seed_twice_is_identical_and_writes_nothing(migrated_engine: Engine) -> None:
    world = build_world(load_config(preset="s"))
    with migrated_engine.begin() as conn:
        first = seed_world(conn, world)
    snap1 = _snapshot(migrated_engine)
    with migrated_engine.begin() as conn:
        second = seed_world(conn, world)
    snap2 = _snapshot(migrated_engine)

    assert first == {name: len(getattr(world, name)) for name, _ in SEED_TABLES}
    assert second == dict.fromkeys(first, 0)
    assert snap1 == snap2  # same contents AND same row versions: no rewrite, no CDC noise


def test_seed_from_fresh_database_is_reproducible(migrated_engine: Engine) -> None:
    """Two independent builds+seeds give byte-identical table contents."""
    cfg = load_config(preset="s")
    with migrated_engine.begin() as conn:
        seed_world(conn, build_world(cfg))
    contents1 = {k: v[:2] for k, v in _snapshot(migrated_engine).items()}
    with migrated_engine.begin() as conn:
        for name, _ in reversed(SEED_TABLES):
            conn.execute(text(f"DELETE FROM {name}"))
        seed_world(conn, build_world(cfg))
    contents2 = {k: v[:2] for k, v in _snapshot(migrated_engine).items()}
    assert contents1 == contents2


def test_reseed_converges_after_drift(migrated_engine: Engine) -> None:
    world = build_world(load_config(preset="s"))
    with migrated_engine.begin() as conn:
        seed_world(conn, world)
        conn.execute(text("UPDATE vehicles SET capacity_kg = 1 WHERE vehicle_id = 'V-0001'"))
    with migrated_engine.begin() as conn:
        written = seed_world(conn, world)
    assert written["vehicles"] == 1
    assert sum(written.values()) == 1
