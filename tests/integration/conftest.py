"""Fixtures for tests against the real TMS Postgres (the Compose container or the CI service).

Each test that needs a clean schema gets its own throwaway database on the same server, so the
developer's `tms` database is never touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

from vlsim.config import SimConfig
from vlsim.tms.schema import metadata
from vlsim.tms.seed import seed_world
from vlsim.tms.settings import tms_dsn
from vlsim.world.build import build_world

ROOT = Path(__file__).resolve().parents[2]
SCRATCH_DB = "vlsim_test"


def alembic_cfg(engine: Engine) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["engine"] = engine
    return cfg


def migrate(engine: Engine, target: str) -> None:
    cfg = alembic_cfg(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if target == "base":
            command.downgrade(cfg, "base")
        else:
            command.upgrade(cfg, target)


class ScratchDatabases:
    """Creates throwaway databases on the TMS server and drops them afterwards."""

    def __init__(self) -> None:
        self.url = make_url(tms_dsn())
        self.admin = create_engine(self.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        self.engines: dict[str, Engine] = {}

    def create(self, name: str) -> Engine:
        with self.admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {name}"))
        self.engines[name] = create_engine(self.url.set(database=name))
        return self.engines[name]

    def close(self) -> None:
        for name, engine in self.engines.items():
            engine.dispose()
            with self.admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        self.admin.dispose()


@pytest.fixture
def scratch_dbs() -> Iterator[ScratchDatabases]:
    dbs = ScratchDatabases()
    try:
        yield dbs
    finally:
        dbs.close()


@pytest.fixture
def scratch_engine(scratch_dbs: ScratchDatabases) -> Engine:
    """An engine on a freshly created, empty database."""
    return scratch_dbs.create(SCRATCH_DB)


def seeded(engine: Engine, cfg: SimConfig) -> Engine:
    """Migrate to head and seed the world for `cfg`."""
    migrate(engine, "head")
    with engine.begin() as conn:
        seed_world(conn, build_world(cfg))
    return engine


BUSINESS_TABLES = ("orders", "shipments", "trips", "trip_shipments", "cod_collections")


def dump(engine: Engine) -> dict[str, list[str]]:
    """Full ordered contents of the tables the simulation writes."""
    out = {}
    with engine.connect() as conn:
        for name in BUSINESS_TABLES:
            pk = ", ".join(c.name for c in metadata.tables[name].primary_key.columns)
            out[name] = list(
                conn.execute(text(f"SELECT t::text FROM {name} t ORDER BY {pk}")).scalars()
            )
    return out


@pytest.fixture
def migrated_engine(scratch_engine: Engine) -> Engine:
    migrate(scratch_engine, "head")
    return scratch_engine
