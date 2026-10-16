"""M3 gate: a toy chain runs identically twice, and checkpoint/restore mid-run is invisible."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.engine import toy
from vlsim.engine.checkpoint import CheckpointStore, checkpoint_hash

START_MS = 1_790_787_600_000  # 2026-10-01T00:00:00+07:00
HOUR_MS = 3_600_000
END_MS = START_MS + 72 * HOUR_MS
FP = "toy-v1"
ROOT = Path(__file__).resolve().parents[3]


def _fresh(seed: int) -> toy.ToyEngine:
    eng = toy.ToyEngine(
        seed=seed, start_ms=START_MS, offset_minutes=420, registry=toy.registry(), fingerprint=FP
    )
    eng.outputs = []
    eng.schedule(START_MS, "toy.order", {})
    return eng


def _digest(lines: list[str]) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode())
        h.update(b"\n")
    return h.hexdigest()


def _uninterrupted(seed: int = 2026) -> tuple[str, str, int]:
    eng = _fresh(seed)
    eng.run(until_ms=END_MS)
    out = eng.outputs
    return _digest(out), checkpoint_hash(eng.to_checkpoint()), len(out)


def test_toy_chain_is_substantial() -> None:
    eng = _fresh(2026)
    eng.run(until_ms=END_MS)
    kinds = {line.split('"kind":"')[1].split('"')[0] for line in eng.outputs}
    assert kinds == {"order", "pickup", "deliver", "audit"}
    assert eng.events_processed > 1_000
    assert len(eng.queue) > 0  # still live: work in flight at the cut-off


def test_same_seed_runs_identically_twice() -> None:
    assert _uninterrupted() == _uninterrupted()


def test_different_seed_differs() -> None:
    assert _uninterrupted(1)[0] != _uninterrupted(2)[0]


@pytest.mark.parametrize("cut_hours", [0, 1, 7, 13, 36, 71])
def test_checkpoint_restore_mid_run_matches_uninterrupted(tmp_path: Path, cut_hours: int) -> None:
    expected_out, expected_ckpt, _ = _uninterrupted()

    first = _fresh(2026)
    first.run(until_ms=START_MS + cut_hours * HOUR_MS)
    store = CheckpointStore(tmp_path / "state.sqlite")
    store.save(first.to_checkpoint())
    store.close()
    emitted_before = first.outputs
    del first  # the "process" dies here

    data = CheckpointStore(tmp_path / "state.sqlite").load_latest()
    assert data is not None
    second = toy.ToyEngine.from_checkpoint(data, toy.registry(), fingerprint=FP)
    second.outputs = []
    second.run(until_ms=END_MS)
    emitted_after = second.outputs

    assert _digest(emitted_before + emitted_after) == expected_out
    assert checkpoint_hash(second.to_checkpoint()) == expected_ckpt


def test_max_events_stop_then_resume_matches(tmp_path: Path) -> None:
    """Stopping between two events at the same instant must also be invisible."""
    expected_out, _, _ = _uninterrupted()
    eng = _fresh(2026)
    eng.run(max_events=777)
    data = eng.to_checkpoint()
    before = eng.outputs
    resumed = toy.ToyEngine.from_checkpoint(data, toy.registry(), fingerprint=FP)
    resumed.outputs = []
    resumed.run(until_ms=END_MS)
    assert _digest(before + resumed.outputs) == expected_out


# Pinned outputs of the toy chain (seed 2026, 72 sim-hours). If this changes, something altered
# RNG streams, event ordering, ID derivation or canonical serialization; that must be deliberate.
# CI runs on x86-64 while development is on arm64, so this also checks cross-arch determinism.
GOLDEN_OUTPUT = "efc2168319ddc46a3cadff124df942ac9d554d4181de2cb200651af20ab0ae88"
GOLDEN_CHECKPOINT = "40106bd852c7a7f18736c1c416a14a80587a91df68aa6bc7e59836d00484b78a"


def test_golden_hashes() -> None:
    out, ckpt, n = _uninterrupted()
    assert (out, ckpt, n) == (GOLDEN_OUTPUT, GOLDEN_CHECKPOINT, 1941)


@pytest.mark.parametrize("hashseed", ["0", "12345"])
def test_independent_of_python_hash_seed(hashseed: str) -> None:
    """A fresh interpreter with a different str-hash seed must give the same output."""
    code = (
        "from tests.unit.engine.test_determinism import _uninterrupted; print(_uninterrupted()[0])"
    )
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    assert res.stdout.strip() == GOLDEN_OUTPUT
