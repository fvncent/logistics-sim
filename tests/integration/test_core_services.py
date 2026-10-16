"""Smoke tests against the Compose core services (`make up`)."""

from __future__ import annotations

import os

import pytest
from confluent_kafka.admin import AdminClient
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.compose_info import pg_settings, pg_version
from vlsim.tms import api
from vlsim.tms.settings import tms_dsn

pytestmark = pytest.mark.integration


def test_tms_health_ok_against_real_postgres() -> None:
    api._engine.cache_clear()
    resp = TestClient(api.app).get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_connected_to_pinned_container_postgres() -> None:
    """Guards against a native host Postgres answering instead of the container (ADR 0004)."""
    engine = create_engine(tms_dsn())
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar_one()
        db = conn.execute(text("SELECT current_database()")).scalar_one()
    engine.dispose()
    expected = pg_version()  # read from compose.yaml, the single source of truth
    assert version.split()[0] == expected, (version, expected)
    assert db == "tms"


def test_cdc_server_settings_match_compose() -> None:
    """wal_level=logical etc. (ADR 0006); CI applies the same values via ALTER SYSTEM."""
    engine = create_engine(tms_dsn())
    with engine.connect() as conn:
        live = {
            k: conn.execute(text(f"SELECT current_setting('{k}')")).scalar_one()
            for k in pg_settings()
        }
    engine.dispose()
    assert live == pg_settings()


@pytest.mark.redpanda
def test_redpanda_reachable() -> None:
    bootstrap = os.environ.get("VLSIM_KAFKA_BOOTSTRAP", "localhost:19092")
    meta = AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=10)
    assert len(meta.brokers) >= 1
