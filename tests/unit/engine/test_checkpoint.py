from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.unit.engine import toy
from vlsim.engine.checkpoint import (
    CheckpointError,
    CheckpointStore,
    canonical_json,
    checkpoint_hash,
)

START_MS = 1_790_787_600_000  # 2026-10-01T00:00:00+07:00
FP = "cfg-fingerprint-1"


def _engine(seed: int = 1) -> toy.ToyEngine:
    eng = toy.ToyEngine(
        seed=seed, start_ms=START_MS, offset_minutes=420, registry=toy.registry(), fingerprint=FP
    )
    eng.outputs = []
    eng.schedule(START_MS, "toy.order", {})
    return eng


def test_canonical_json_is_key_order_independent() -> None:
    a = canonical_json({"b": 1, "a": {"y": [1, 2.5], "x": None}})
    b = canonical_json({"a": {"x": None, "y": [1, 2.5]}, "b": 1})
    assert a == b == '{"a":{"x":null,"y":[1,2.5]},"b":1}'


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})


def test_checkpoint_contains_required_parts() -> None:
    eng = _engine()
    eng.run(until_ms=START_MS + 6 * 3_600_000)
    data = eng.to_checkpoint()
    assert {"format", "fingerprint", "seed", "clock_ms", "next_seq", "queue", "rng", "counters",
            "state", "events_processed"} <= set(data)  # fmt: skip
    assert data["queue"] == sorted(data["queue"], key=lambda e: (e[0], e[1]))
    assert set(data["rng"]) == {"toy.arrivals", "toy.pickup", "toy.travel", "toy.outcome"}
    assert data["counters"]["order"] == data["state"]["orders"]


def test_round_trip_in_memory_is_identical() -> None:
    eng = _engine()
    eng.run(until_ms=START_MS + 6 * 3_600_000)
    data = eng.to_checkpoint()
    restored = toy.ToyEngine.from_checkpoint(data, toy.registry(), fingerprint=FP)
    assert restored.to_checkpoint() == data
    assert checkpoint_hash(restored.to_checkpoint()) == checkpoint_hash(data)


def test_sqlite_store_round_trip_and_latest(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "state.sqlite")
    eng = _engine()
    eng.run(until_ms=START_MS + 3_600_000)
    h1 = store.save(eng.to_checkpoint())
    eng.run(until_ms=START_MS + 2 * 3_600_000)
    h2 = store.save(eng.to_checkpoint())
    latest = store.load_latest()
    assert latest is not None
    assert checkpoint_hash(latest) == h2 != h1
    assert latest == eng.to_checkpoint()
    store.close()


def test_store_body_is_canonical_json_not_pickle(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = CheckpointStore(path)
    eng = _engine()
    eng.run(until_ms=START_MS + 3_600_000)
    store.save(eng.to_checkpoint())
    store.close()
    with sqlite3.connect(path) as conn:
        body, digest = conn.execute("SELECT body, sha256 FROM checkpoints").fetchone()
    assert isinstance(body, str)
    assert canonical_json(json.loads(body)) == body
    assert digest == checkpoint_hash(json.loads(body))


def test_tampered_checkpoint_detected(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = CheckpointStore(path)
    eng = _engine()
    eng.run(until_ms=START_MS + 3_600_000)
    store.save(eng.to_checkpoint())
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE checkpoints SET body = replace(body, '\"seed\":1', '\"seed\":2')")
    with pytest.raises(CheckpointError, match="hash"):
        CheckpointStore(path).load_latest()


def test_store_prunes_old_checkpoints(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "s.sqlite", keep=2)
    eng = _engine()
    for h in range(1, 5):
        eng.run(until_ms=START_MS + h * 3_600_000)
        store.save(eng.to_checkpoint())
    assert store.count() == 2
    store.close()


def test_empty_store_returns_none(tmp_path: Path) -> None:
    assert CheckpointStore(tmp_path / "s.sqlite").load_latest() is None


def test_restore_refuses_other_config_or_unknown_handler() -> None:
    eng = _engine()
    eng.run(until_ms=START_MS + 3_600_000)
    data = eng.to_checkpoint()
    with pytest.raises(CheckpointError, match="fingerprint"):
        toy.ToyEngine.from_checkpoint(data, toy.registry(), fingerprint="other")
    from vlsim.engine.core import HandlerRegistry  # noqa: PLC0415

    with pytest.raises(CheckpointError, match="handler"):
        toy.ToyEngine.from_checkpoint(data, HandlerRegistry(), fingerprint=FP)
