from __future__ import annotations

import json
import urllib.request

from vlsim.ops.health import HealthChecker
from vlsim.ops.metrics import SimMetrics
from vlsim.ops.server import OpsServer


def _checker(pg: bool, rp: bool, backlog: int) -> HealthChecker:
    return HealthChecker(
        postgres_ok=lambda: pg, redpanda_ok=lambda: rp, spill_backlog=lambda: backlog
    )


def test_health_states() -> None:
    assert _checker(True, True, 0).status()["status"] == "ok"
    assert _checker(True, False, 1234).status()["status"] == "degraded"
    assert _checker(True, True, 1234).status()["status"] == "degraded"  # still spilling
    failing = _checker(False, True, 0).status()
    assert failing["status"] == "failing" and failing["postgres"] == "unreachable"


def test_metrics_exposition_contains_required_series() -> None:
    m = SimMetrics()
    m.update(
        sim_time_ms=1_790_787_600_000, events_total=10, queue_depth=5,
        emitted={"tracking.scans": 3, "telemetry.gps": 7}, spill_backlog_bytes=0,
        flush_lag_sim_s=1.0, flush_lag_wall_s=0.2, checkpoint_age_sim_s=60.0,
        checkpoint_age_wall_s=1.0, drift_s=0.1, events_per_s=50.0, speed=60.0,
    )  # fmt: skip
    m.update(
        sim_time_ms=1_790_787_601_000, events_total=15, queue_depth=4,
        emitted={"tracking.scans": 4, "telemetry.gps": 9}, spill_backlog_bytes=10,
        flush_lag_sim_s=0.0, flush_lag_wall_s=0.0, checkpoint_age_sim_s=0.0,
        checkpoint_age_wall_s=0.0, drift_s=0.0, events_per_s=40.0, speed=60.0,
    )  # fmt: skip
    text = m.exposition().decode()
    for series in (
        "vlsim_sim_time_seconds 1.790787601e+09",
        "vlsim_engine_events_total 15.0",
        "vlsim_events_per_second 40.0",
        "vlsim_queue_depth 4.0",
        'vlsim_emitted_events_total{topic="telemetry.gps"} 9.0',
        "vlsim_spill_backlog_bytes 10.0",
        "vlsim_flush_lag_sim_seconds",
        "vlsim_flush_lag_wall_seconds",
        "vlsim_checkpoint_age_sim_seconds",
        "vlsim_checkpoint_age_wall_seconds",
        "vlsim_pacing_drift_seconds",
        "vlsim_speed 60.0",
    ):
        assert series in text, series


def test_ops_server_routes() -> None:
    speed = {"x": 60.0}
    srv = OpsServer(
        port=0,
        metrics=SimMetrics(),
        health=_checker(False, True, 0),
        get_speed=lambda: speed["x"],
        set_speed=lambda v: speed.__setitem__("x", v),
    )
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        assert b"vlsim_speed" in urllib.request.urlopen(f"{base}/metrics").read()
        try:
            urllib.request.urlopen(f"{base}/health")
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503 and json.loads(e.read())["status"] == "failing"
        req = urllib.request.Request(f"{base}/speed?x=250", method="POST")
        assert json.loads(urllib.request.urlopen(req).read()) == {"speed": 250.0}
        assert speed["x"] == 250.0
        bad = urllib.request.Request(f"{base}/speed?x=5000", method="POST")
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.stop()
