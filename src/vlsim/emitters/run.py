"""A run = one simulation history written to ground truth and topics.

`<ground_truth_root>/<run_id>/run.json` records the config fingerprint and the topic offsets at
the moment the run started, so reconcile can scope topic contents to this run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MANIFEST = "run.json"


def create_run(
    run_dir: Path, *, run_id: str, fingerprint: str, start_offsets: dict[str, Any]
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / MANIFEST).exists() or any(run_dir.glob("*/*.jsonl")):
        raise RuntimeError(f"{run_dir} already holds a run; resume it or choose a new run id")
    manifest = {"run_id": run_id, "fingerprint": fingerprint, "start_offsets": start_offsets}
    (run_dir / MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2))


def has_run(run_dir: Path) -> bool:
    return (run_dir / MANIFEST).exists()


def load_run(run_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run_dir / MANIFEST).read_text())
    return data
