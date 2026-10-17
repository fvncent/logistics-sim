"""Assembly: wire config, TMS, checkpoint store and the emitting pipeline into a Simulation.

Lives outside the engine-side packages because it touches every layer (including Kafka admin).
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import structlog
from confluent_kafka import KafkaException
from confluent_kafka.admin import AdminClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from vlsim.config import SimConfig, TransportConfig
from vlsim.emitters.ground_truth import GroundTruthWriter
from vlsim.emitters.kafka import SpillingEmitter
from vlsim.emitters.pipeline import EmitPipeline
from vlsim.emitters.run import create_run, has_run, load_run
from vlsim.emitters.topics import end_offsets, ensure_topics, make_producer
from vlsim.engine.checkpoint import CheckpointStore
from vlsim.engine.live import LiveRunner, Pacer, WallClockFlush
from vlsim.models.routes import _offset_minutes
from vlsim.models.sim import Simulation
from vlsim.ops.health import HealthChecker
from vlsim.ops.metrics import SimMetrics
from vlsim.ops.server import OpsServer
from vlsim.ports import TmsService
from vlsim.tms.seed import seed_world
from vlsim.tms.service import PostgresTms
from vlsim.world.build import build_world

log = structlog.get_logger()


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
        resuming = store.load_latest() is not None
        if has_run(run_dir):
            if load_run(run_dir)["fingerprint"] != cfg.fingerprint():
                raise RuntimeError(f"run {run_id} was started with a different config")
            if not resuming:
                # Killed before its first checkpoint: restart this run from scratch. Keep the
                # manifest (start offsets) and drop its ground truth; replay regenerates the
                # same events with the same ids, and Postgres upserts absorb what was written.
                GroundTruthWriter(run_dir).truncate_to({})
                log.warning("run_restarted_from_scratch", run_id=run_id)
        elif resuming:
            raise RuntimeError(f"checkpoint exists but run {run_id} has no manifest in {run_dir}")
        else:
            create_run(
                run_dir,
                run_id=run_id,
                fingerprint=cfg.fingerprint(),
                start_offsets=end_offsets(bootstrap, topics),
            )
        sink = SpillingEmitter(
            make_producer(bootstrap, transport), state_dir / "spill.jsonl", transport.emitter
        )
        pipeline = EmitPipeline(
            cfg, _offset_minutes(cfg.base.timezone_offset), GroundTruthWriter(run_dir), sink
        )
    return Simulation.open(cfg, tms, store, pipeline)


def run_live(
    cfg: SimConfig,
    *,
    db_url: str,
    state_dir: Path,
    transport: TransportConfig,
    bootstrap: str | None,
    ground_truth_root: Path,
    run_id: str,
    speed: float,
    stop_at_ms: int | None,
    http_port: int | None,
) -> int:
    """Live mode: seed (idempotent), open/resume, serve /metrics /health /speed, pace at
    `speed`, stop gracefully on SIGTERM/SIGINT (final checkpoint), return exit code 0."""
    db = create_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    with db.begin() as conn:
        seed_world(conn, build_world(cfg))
    tms = PostgresTms(db)
    sim = open_simulation(
        cfg,
        tms,
        state_dir=state_dir,
        transport=transport,
        bootstrap=bootstrap,
        ground_truth_root=ground_truth_root,
        run_id=run_id,
    )
    flush_policy = WallClockFlush(cfg.operations.runner.live_flush_wall_seconds)
    sim.flush_policy = flush_policy
    pacer = Pacer(speed, sim.now_ms())
    sink = sim.pipeline.sink if sim.pipeline is not None else None
    state: dict[str, Any] = {"failing": False, "requested_speed": None, "ckpt_seen": -1,
                             "ckpt_wall": 0.0, "last_metrics_wall": 0.0}  # fmt: skip
    metrics = SimMetrics()

    def observe(stats: dict[str, float]) -> None:
        state["failing"] = bool(stats["failing"])
        if state["requested_speed"] is not None:
            pacer.set_speed(state["requested_speed"], sim.now_ms())
            log.info("speed_changed", speed=state["requested_speed"])
            state["requested_speed"] = None
        if sim.checkpoints_saved != state["ckpt_seen"]:
            state["ckpt_seen"], state["ckpt_wall"] = sim.checkpoints_saved, stats["wall_s"]
            if sim.checkpoints_saved:
                log.info("checkpoint_saved", sim_time=sim.engine.clock.isoformat(),
                         n=sim.checkpoints_saved)  # fmt: skip
        if stats["wall_s"] - state["last_metrics_wall"] < 0.5:
            return
        state["last_metrics_wall"] = stats["wall_s"]
        now = sim.now_ms()
        released = sim.pipeline.released if sim.pipeline is not None else {}
        topics = cfg.emitters.topics
        metrics.update(
            sim_time_ms=now,
            events_total=sim.engine.events_processed,
            queue_depth=len(sim.engine.queue),
            emitted={topics.scans: released.get("scans", 0), topics.gps: released.get("gps", 0)},
            spill_backlog_bytes=_backlog(sink),
            flush_lag_sim_s=(now - sim.last_flush_ms) / 1000,
            flush_lag_wall_s=flush_policy.wall_since_flush(),
            checkpoint_age_sim_s=(now - sim.last_checkpoint_ms) / 1000,
            checkpoint_age_wall_s=stats["wall_s"] - state["ckpt_wall"],
            drift_s=stats["drift_s"],
            events_per_s=stats["events_per_s"],
            speed=stats["speed"],
        )

    def postgres_ok() -> bool:
        if state["failing"]:
            return False
        try:
            with db.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def redpanda_ok() -> bool:
        if not bootstrap:
            return True
        try:
            AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=2)
            return True
        except KafkaException:
            return False

    runner = LiveRunner(
        sim,
        pacer,
        stop_at_ms=stop_at_ms,
        transient=(OperationalError, InterfaceError),
        observe=observe,
    )
    server = None
    if http_port is not None:
        server = OpsServer(
            port=http_port,
            metrics=metrics,
            health=HealthChecker(
                postgres_ok=postgres_ok,
                redpanda_ok=redpanda_ok,
                spill_backlog=lambda: _backlog(sink),
            ),
            get_speed=lambda: state["requested_speed"] or pacer.speed,
            set_speed=lambda v: state.__setitem__("requested_speed", v),
        )
        server.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: runner.stop())
    log.info("live_start", sim_time=sim.engine.clock.isoformat(), speed=speed, run_id=run_id,
             http_port=None if server is None else server.port)  # fmt: skip
    runner.run()  # returns after sim.close(): final checkpoint, DB committed, producer flushed
    if server is not None:
        server.stop()
    log.info("live_stopped", sim_time=sim.engine.clock.isoformat(),
             events=sim.engine.events_processed, checkpoints=sim.checkpoints_saved,
             emitter=dict(getattr(sink, "stats", {})))  # fmt: skip
    return 0


def _backlog(sink: Any) -> int:
    return int(sink.backlog_bytes()) if sink is not None else 0
