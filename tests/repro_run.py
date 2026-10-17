"""Run one S-scale sim day with in-memory TMS and broker fakes; print output hashes as JSON.

Used by tests/unit/test_reproducibility.py in fresh interpreters, and by hand to (re)generate
the per-architecture golden hashes:  uv run python -m tests.repro_run
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from tests.fakes import InMemoryTms
from tests.unit.emitters.fake_broker import FakeProducer
from vlsim.config import load_config, load_transport
from vlsim.emitters.ground_truth import GroundTruthWriter
from vlsim.emitters.kafka import SpillingEmitter
from vlsim.emitters.pipeline import EmitPipeline
from vlsim.engine.checkpoint import CheckpointStore, checkpoint_hash
from vlsim.engine.clock import DAY_MS, ms_from_datetime
from vlsim.models.sim import Simulation


def run(days: float = 1.0) -> dict[str, str]:
    cfg = load_config(preset="s")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        gt = GroundTruthWriter(root / "gt")
        sink = SpillingEmitter(FakeProducer(), root / "spill.jsonl", load_transport().emitter)
        tms = InMemoryTms(cfg)
        sim = Simulation.fresh(
            cfg, tms, CheckpointStore(root / "c.sqlite"), EmitPipeline(cfg, 420, gt, sink)
        )
        sim.run_until(ms_from_datetime(cfg.base.start_time) + int(days * DAY_MS))
        sim.close()
        out = {
            "machine": f"{platform.system()}-{platform.machine()}",
            "numpy": __import__("numpy").__version__,
        }
        reader = GroundTruthWriter(root / "gt")
        for topic in (cfg.emitters.topics.scans, cfg.emitters.topics.gps):
            h = hashlib.sha256()
            for line in reader.read_lines(topic):  # decompressed content, independent of gzip
                h.update(line.encode())
                h.update(b"\n")
            out[topic] = h.hexdigest()
        out["tms"] = hashlib.sha256(repr(tms.snapshot()).encode()).hexdigest()
        out["engine_checkpoint"] = checkpoint_hash(sim.engine.to_checkpoint())
        reader.close()
        return out


GOLDEN = Path(__file__).resolve().parent / "golden" / "repro_hashes.json"
KEYS = ("tracking.scans", "telemetry.gps", "tms", "engine_checkpoint")


def write_golden() -> None:
    """Deliberately record this platform's hashes. Refuses under CI: goldens are never
    updated from a CI run."""
    if os.environ.get("CI"):
        sys.exit("refusing to write golden hashes under CI")
    out = run()
    golden = json.loads(GOLDEN.read_text())
    golden[out["machine"]] = {k: out[k] for k in KEYS}
    GOLDEN.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
    print(f"wrote golden hashes for {out['machine']}; commit with the reason for the change")


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-golden"]:
        write_golden()
    else:
        json.dump(run(), sys.stdout, sort_keys=True)
