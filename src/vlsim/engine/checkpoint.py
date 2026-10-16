"""Checkpoints: canonical JSON (never pickle) in an append-only SQLite file.

The body is the canonical JSON of Engine.to_checkpoint(); its SHA-256 is stored alongside and
verified on load. SQLite is the simulator's internal state store (SPEC), not the TMS database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from vlsim.engine.canonical import canonical_json
from vlsim.engine.core import CheckpointError

__all__ = ["CheckpointError", "CheckpointStore", "canonical_json", "checkpoint_hash"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_time_ms      INTEGER NOT NULL,
    events_processed INTEGER NOT NULL,
    sha256           TEXT    NOT NULL,
    body             TEXT    NOT NULL
)
"""


def checkpoint_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


class CheckpointStore:
    def __init__(self, path: Path, keep: int = 5) -> None:
        if keep < 1:
            raise ValueError("keep must be >= 1")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self._conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # a saved checkpoint survives power loss
        self._conn.execute(_SCHEMA)

    def save(self, data: dict[str, Any]) -> str:
        body = canonical_json(data)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._tx():
            self._conn.execute(
                "INSERT INTO checkpoints (sim_time_ms, events_processed, sha256, body) "
                "VALUES (?, ?, ?, ?)",
                (data["clock_ms"], data["events_processed"], digest, body),
            )
            self._conn.execute(
                "DELETE FROM checkpoints WHERE id NOT IN "
                "(SELECT id FROM checkpoints ORDER BY id DESC LIMIT ?)",
                (self.keep,),
            )
        return digest

    def load_latest(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT sha256, body FROM checkpoints ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        digest, body = row
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
            raise CheckpointError("checkpoint hash mismatch: body was modified or corrupted")
        data: dict[str, Any] = json.loads(body)
        return data

    def count(self) -> int:
        n: int = self._conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        return n

    def close(self) -> None:
        self._conn.close()

    def _tx(self) -> _Tx:
        return _Tx(self._conn)


class _Tx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
