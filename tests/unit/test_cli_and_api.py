from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vlsim.cli import main
from vlsim.tms import api


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
