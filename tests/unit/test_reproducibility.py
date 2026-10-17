"""Same seed + config => identical ground-truth output (SPEC: "Run this in CI").

Two fresh interpreters (different PYTHONHASHSEED) must agree exactly. Their hashes must also match
the golden hashes recorded for this CPU architecture in tests/golden/repro_hashes.json.
arm64 and amd64 hashes MAY differ: numpy/libm transcendental functions (sin, cos, exp, log) can
round differently per architecture and SIMD path, and those last-bit differences can shift a
rounded coordinate or a millisecond. Determinism is guaranteed per architecture, not across
architectures (README, ADR 0012).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "repro_hashes.json"
KEYS = ("tracking.scans", "telemetry.gps", "tms", "engine_checkpoint")


def _run(hashseed: str) -> dict[str, str]:
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    res = subprocess.run(
        [sys.executable, "-m", "tests.repro_run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, str] = json.loads(res.stdout)
    return out


@pytest.fixture(scope="module")
def runs() -> tuple[dict[str, str], dict[str, str]]:
    return _run("0"), _run("4242")


def test_two_runs_identical(runs: tuple[dict[str, str], dict[str, str]]) -> None:
    a, b = runs
    assert {k: a[k] for k in KEYS} == {k: b[k] for k in KEYS}


def test_matches_golden_for_this_architecture(runs: tuple[dict[str, str], dict[str, str]]) -> None:
    got = runs[0]
    golden = json.loads(GOLDEN.read_text())
    machine = f"{platform.system()}-{platform.machine()}"  # libm differs by OS too
    entry = json.dumps({machine: {k: got[k] for k in KEYS}}, indent=2)
    if machine not in golden:
        pytest.fail(f"no golden hashes for {machine}; if intended, add:\n{entry}")
    want = golden[machine]
    assert {k: got[k] for k in KEYS} == {k: want[k] for k in KEYS}, (
        f"output changed on {machine} (numpy {got['numpy']}). If deliberate, update the golden:\n"
        + entry
    )
