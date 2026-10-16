"""Smoke tests against the Compose core services (`make up`)."""

from __future__ import annotations

import os

import pytest
from confluent_kafka.admin import AdminClient
from fastapi.testclient import TestClient

from vlsim.tms import api

pytestmark = pytest.mark.integration


def test_tms_health_ok_against_real_postgres() -> None:
    api._engine.cache_clear()
    resp = TestClient(api.app).get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "db": "ok"}


@pytest.mark.redpanda
def test_redpanda_reachable() -> None:
    bootstrap = os.environ.get("VLSIM_KAFKA_BOOTSTRAP", "localhost:19092")
    meta = AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=10)
    assert len(meta.brokers) >= 1
