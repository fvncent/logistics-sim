"""Smoke tests against the Compose core services (`make up`)."""

from __future__ import annotations

import os

import pytest
from confluent_kafka.admin import AdminClient
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

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
        version = conn.execute(text("SELECT version()")).scalar_one()
        db = conn.execute(text("SELECT current_database()")).scalar_one()
    engine.dispose()
    assert version.startswith("PostgreSQL 17.11 "), version  # pinned in compose.yaml and ci.yml
    assert db == "tms"


@pytest.mark.redpanda
def test_redpanda_reachable() -> None:
    bootstrap = os.environ.get("VLSIM_KAFKA_BOOTSTRAP", "localhost:19092")
    meta = AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=10)
    assert len(meta.brokers) >= 1
