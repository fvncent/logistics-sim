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

from vlsim.tms.settings import tms_dsn

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


@pytest.fixture
def scratch_engine() -> Iterator[Engine]:
    """An engine on a freshly created, empty database."""
    url = make_url(tms_dsn())
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    engine = create_engine(url.set(database=SCRATCH_DB))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def migrated_engine(scratch_engine: Engine) -> Engine:
    migrate(scratch_engine, "head")
    return scratch_engine
