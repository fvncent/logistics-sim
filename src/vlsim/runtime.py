"""Assembly: wire config, TMS, checkpoint store and the emitting pipeline into a Simulation.

Lives outside the engine-side packages because it touches every layer (including Kafka admin).
"""

from __future__ import annotations

from pathlib import Path

from vlsim.config import SimConfig, TransportConfig
from vlsim.emitters.ground_truth import GroundTruthWriter
from vlsim.emitters.kafka import SpillingEmitter
from vlsim.emitters.pipeline import EmitPipeline
from vlsim.emitters.run import create_run, load_run
from vlsim.emitters.topics import end_offsets, ensure_topics, make_producer
from vlsim.engine.checkpoint import CheckpointStore
from vlsim.models.routes import _offset_minutes
from vlsim.models.sim import Simulation
from vlsim.ports import TmsService


def open_simulation(
    cfg: SimConfig,
    tms: TmsService,
    *,
    state_dir: Path,
    transport: TransportConfig,
    bootstrap: str | None,
    ground_truth_root: Path,
    run_id: str,
) -> Simulation:
    """Resume from `state_dir` if it holds a checkpoint, otherwise start a fresh run.

    With a bootstrap server, events go to ground truth + Redpanda; a fresh run records the
    current topic end offsets in `<ground_truth_root>/<run_id>/run.json` for reconcile.
    """
    store = CheckpointStore(state_dir / "checkpoints.sqlite")
    pipeline = None
    if bootstrap:
        topics = [cfg.emitters.topics.scans, cfg.emitters.topics.gps]
        ensure_topics(bootstrap, transport, topics)
        run_dir = ground_truth_root / run_id
        if store.load_latest() is None:
            create_run(
                run_dir,
                run_id=run_id,
                fingerprint=cfg.fingerprint(),
                start_offsets=end_offsets(bootstrap, topics),
            )
        elif load_run(run_dir)["fingerprint"] != cfg.fingerprint():
            raise RuntimeError(f"run {run_id} was started with a different config")
        sink = SpillingEmitter(
            make_producer(bootstrap, transport), state_dir / "spill.jsonl", transport.emitter
        )
        pipeline = EmitPipeline(
            cfg, _offset_minutes(cfg.base.timezone_offset), GroundTruthWriter(run_dir), sink
        )
    return Simulation.open(cfg, tms, store, pipeline)
