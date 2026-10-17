"""M6 gates on a real `vlsim run` process (1000x, Postgres + Redpanda): SIGTERM exits 0 after
a final checkpoint; restart resumes; kill -9 then restart also resumes; reconcile passes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tests.integration.conftest import ROOT, ScratchDatabases, migrate
from vlsim.validation.reconcile import reconcile

pytestmark = [pytest.mark.integration, pytest.mark.redpanda]
BOOTSTRAP = os.environ.get("VLSIM_KAFKA_BOOTSTRAP", "127.0.0.1:19092")
STOP_AT_DAYS = "0.4"  # ~35 s of wall time at 1000x


def _start(db: Engine, root: Path, run_id: str) -> subprocess.Popen[str]:
    env = {**os.environ, "VLSIM_TMS_DSN": db.url.render_as_string(hide_password=False)}
    cmd = [sys.executable, "-m", "vlsim.cli", "run", "--preset", "s", "--speed", "1000",
           "--stop-at-sim-days", STOP_AT_DAYS, "--run-id", run_id, "--http-port", "-1",
           "--state-dir", str(root / "state"), "--ground-truth-dir", str(root / "gt"),
           "--bootstrap", BOOTSTRAP]  # fmt: skip
    return subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def _events(proc: subprocess.Popen[str], until: str, n: int) -> list[dict[str, object]]:
    """Read JSON log lines until `until` has been seen n times (no sleeping, no wall clock)."""
    assert proc.stdout is not None
    seen, out = 0, []
    for line in proc.stdout:
        if not line.startswith("{"):
            continue
        ev = json.loads(line)
        out.append(ev)
        if ev.get("event") == until:
            seen += 1
            if seen >= n:
                return out
    raise AssertionError(f"process ended before {n}x {until}: {out[-5:]}")


def _finish(proc: subprocess.Popen[str]) -> list[dict[str, object]]:
    assert proc.stdout is not None
    rest = [json.loads(x) for x in proc.stdout if x.startswith("{")]
    assert proc.wait(timeout=180) == 0, rest[-5:]
    return rest


@pytest.fixture
def db(scratch_dbs: ScratchDatabases) -> Engine:
    engine = scratch_dbs.create("vlsim_live")
    migrate(engine, "head")
    return engine


def test_sigterm_then_restart_resumes_and_reconciles(db: Engine, tmp_path: Path) -> None:
    proc = _start(db, tmp_path, "sigterm")
    _events(proc, "checkpoint_saved", 3)
    proc.send_signal(signal.SIGTERM)
    tail = _finish(proc)  # exit code 0 asserted inside
    assert any(e.get("event") == "live_stopped" for e in tail)

    proc = _start(db, tmp_path, "sigterm")
    started = _events(proc, "live_start", 1)[-1]
    assert started["sim_time"] != "2026-10-01T00:00:00.000+07:00"  # resumed, not restarted
    _finish(proc)
    report = reconcile(tmp_path / "gt" / "sigterm", BOOTSTRAP, db)
    assert report["ok"], report


def test_kill9_then_restart_resumes_and_reconciles(db: Engine, tmp_path: Path) -> None:
    proc = _start(db, tmp_path, "kill9")
    _events(proc, "checkpoint_saved", 3)
    _events(proc, "checkpoint_saved", 1)  # let it run past the checkpoint a little
    proc.kill()  # SIGKILL: no handler, no final checkpoint
    proc.wait(timeout=30)
    assert proc.returncode == -signal.SIGKILL

    proc = _start(db, tmp_path, "kill9")
    _finish(proc)
    report = reconcile(tmp_path / "gt" / "kill9", BOOTSTRAP, db)
    assert report["ok"], report
