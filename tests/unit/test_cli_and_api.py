from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vlsim.cli import main
from vlsim.tms import api
from vlsim.tms.settings import tms_dsn


def test_config_check_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config-check", "--preset", "m"]) == 0
    assert '"preset": "m"' in capsys.readouterr().out


def test_health_reports_unreachable_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLSIM_TMS_DSN", "postgresql+psycopg://x:y@127.0.0.1:1/none")
    api._engine.cache_clear()
    resp = TestClient(api.app).get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"] == "unreachable"
    api._engine.cache_clear()


def test_dsn_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLSIM_TMS_DSN", raising=False)
    monkeypatch.delenv("TMS_PG_PORT", raising=False)
    assert tms_dsn().endswith("@localhost:55432/tms")
    monkeypatch.setenv("TMS_PG_PORT", "6543")
    assert tms_dsn().endswith("@localhost:6543/tms")
    monkeypatch.setenv("VLSIM_TMS_DSN", "postgresql+psycopg://a:b@h:1/x")
    assert tms_dsn() == "postgresql+psycopg://a:b@h:1/x"
