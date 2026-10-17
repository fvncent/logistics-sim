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


def _mismatched(a: dict[str, str], b: dict[str, str]) -> list[str]:
    return [k for k in KEYS if a.get(k) != b.get(k)]


def test_two_runs_identical(runs: tuple[dict[str, str], dict[str, str]]) -> None:
    bad = _mismatched(*runs)
    if bad:  # no hashes in the message: there is nothing to "accept" here, it is a bug
        pytest.fail(f"NON-DETERMINISM: two fresh runs differ in {bad}", pytrace=False)


def test_matches_golden_for_this_architecture(runs: tuple[dict[str, str], dict[str, str]]) -> None:
    """Fails loudly and never prints hashes, so a mismatch cannot be "fixed" by pasting the
    new value from a CI log. Regenerating goldens is a deliberate, local-only step:
    `uv run python -m tests.repro_run --write-golden` (refuses to run under CI)."""
    got = runs[0]
    golden = json.loads(GOLDEN.read_text())
    machine = f"{platform.system()}-{platform.machine()}"  # libm differs by OS too
    if machine not in golden:
        pytest.fail(
            f"REPRODUCIBILITY: no golden hashes recorded for {machine}. Record them on a "
            "trusted machine of that platform with `python -m tests.repro_run --write-golden`.",
            pytrace=False,
        )
    bad = _mismatched(got, golden[machine])
    if bad:
        pytest.fail(
            f"REPRODUCIBILITY FAILURE on {machine} (numpy {got['numpy']}): output differs from "
            f"the recorded golden in {bad}. Same seed + config must give identical output. "
            "Find the cause (model change, dependency bump, platform maths); only if the change "
            "is intended, regenerate locally with --write-golden and say why in the commit.",
            pytrace=False,
        )
