"""M5 gates against real Redpanda + Postgres: clean run reconciles; a broker pause mid-run
loses nothing and crashes nothing; a hard crash + resume still reconciles."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.conftest import ROOT, ScratchDatabases, dump, seeded
from tests.unit.emitters.fake_broker import FakeProducer
from vlsim.config import SimConfig, TransportConfig, load_config, load_transport
from vlsim.emitters.ground_truth import GroundTruthWriter
from vlsim.emitters.kafka import SpillingEmitter
from vlsim.emitters.pipeline import EmitPipeline
from vlsim.engine.checkpoint import CheckpointStore
from vlsim.engine.clock import DAY_MS, HOUR_MS, ms_from_datetime
from vlsim.models.sim import Simulation
from vlsim.runtime import open_simulation
from vlsim.tms.service import PostgresTms
from vlsim.validation.reconcile import reconcile

pytestmark = [pytest.mark.integration, pytest.mark.redpanda]
BOOTSTRAP = os.environ.get("VLSIM_KAFKA_BOOTSTRAP", "127.0.0.1:19092")
TOPICS = ("tracking.scans", "telemetry.gps")


def _docker() -> str:
    found = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
    return found


def _compose(*args: str) -> None:
    subprocess.run([_docker(), "compose", "--profile", "core", *args], cwd=ROOT, check=True)


@pytest.fixture(scope="module")
def cfg() -> SimConfig:
    return load_config(preset="s")


def _open(
    cfg: SimConfig, db: Any, root: Path, run_id: str, transport: TransportConfig
) -> Simulation:
    return open_simulation(
        cfg,
        PostgresTms(db),
        state_dir=root / "state",
        transport=transport,
        bootstrap=BOOTSTRAP,
        ground_truth_root=root / "gt",
        run_id=run_id,
    )


def _sink(sim: Simulation) -> SpillingEmitter:
    assert sim.pipeline is not None and isinstance(sim.pipeline.sink, SpillingEmitter)
    return sim.pipeline.sink


def _start(cfg: SimConfig) -> int:
    return ms_from_datetime(cfg.base.start_time)


def test_clean_run_reconciles(
    cfg: SimConfig, scratch_dbs: ScratchDatabases, tmp_path: Path
) -> None:
    db = seeded(scratch_dbs.create("vlsim_emit"), cfg)
    sim = _open(cfg, db, tmp_path, "clean", load_transport())
    sim.run_until(_start(cfg) + 2 * DAY_MS)
    sim.close()
    report = reconcile(tmp_path / "gt" / "clean", BOOTSTRAP, db)
    assert report["ok"], report
    gps = report["topics"]["telemetry.gps"]
    assert gps["ground_truth_events"] > 100_000  # ~220k pings/day at S (SPEC estimate)
    assert report["postgres"]["shipments_in_db"] == report["postgres"]["shipments_in_ground_truth"]


def test_redpanda_pause_loses_nothing_and_does_not_crash(
    cfg: SimConfig, scratch_dbs: ScratchDatabases, tmp_path: Path
) -> None:
    base = load_transport()
    transport = base.model_copy(
        update={
            "producer": {**base.producer, "message.timeout.ms": 5000},
            "emitter": base.emitter.model_copy(
                update={"max_inflight": 2_000, "checkpoint_flush_timeout_s": 1.0}
            ),
        }
    )
    db = seeded(scratch_dbs.create("vlsim_pause"), cfg)
    sim = _open(cfg, db, tmp_path, "pause", transport)
    sim.run_until(_start(cfg) + 12 * HOUR_MS)
    _compose("pause", "redpanda")
    try:
        sim.run_until(_start(cfg) + 30 * HOUR_MS)  # keeps simulating, through checkpoints
        sink = _sink(sim)
        for _ in range(16):  # let librdkafka's message timeouts fire (blocking poll, no sleep)
            sink.producer.poll(0.5)
        sink.service()
        assert sink.stats["spilled"] > 0, dict(sink.stats)
        assert sink.backlog_bytes() > 0
    finally:
        _compose("unpause", "redpanda")
    sim.run_until(_start(cfg) + 2 * DAY_MS)
    sim.close()
    report = reconcile(tmp_path / "gt" / "pause", BOOTSTRAP, db)
    assert report["ok"], report


def test_hard_crash_and_resume_still_reconciles(
    cfg: SimConfig, scratch_dbs: ScratchDatabases, tmp_path: Path
) -> None:
    db = seeded(scratch_dbs.create("vlsim_emit_crash"), cfg)
    first = _open(cfg, db, tmp_path, "crash", load_transport())
    first.run_until(_start(cfg) + DAY_MS)  # last checkpoint at 24 h
    first.engine.run(until_ms=first.engine.clock.now_ms + 5 * HOUR_MS)
    first.flush_outputs()  # post-checkpoint writes committed, events in ground truth + topics
    _sink(first).producer.flush(10)
    first.engine.run(until_ms=first.engine.clock.now_ms + HOUR_MS)
    first.engine.ctx.tms.discard_buffer()  # type: ignore[attr-defined]
    del first  # killed: outbox, buffers and producer queue are gone

    resumed = _open(cfg, db, tmp_path, "crash", load_transport())
    assert resumed.engine.clock.now_ms == _start(cfg) + DAY_MS
    resumed.run_until(_start(cfg) + 2 * DAY_MS)
    resumed.close()
    report = reconcile(tmp_path / "gt" / "crash", BOOTSTRAP, db)
    assert report["ok"], report
    assert report["topics"]["tracking.scans"]["duplicates_in_topic"] > 0  # replayed hours


def test_kill_before_first_checkpoint_restarts_run_and_reconciles(
    cfg: SimConfig, scratch_dbs: ScratchDatabases, tmp_path: Path
) -> None:
    """Killed before its first checkpoint, with leftovers everywhere: ground-truth lines on disk,
    events in the topic, committed Postgres rows, and later writes that never committed. The
    restart must not refuse the run; it must end byte-identical (ground truth) and row-identical
    (Postgres) to an uninterrupted run, and reconcile."""
    end = _start(cfg) + 18 * HOUR_MS  # first trips leave after the 10:00 pickup is sorted
    ref_db = seeded(scratch_dbs.create("vlsim_early_ref"), cfg)
    ref_root = tmp_path / "ref"
    ref_pipeline = EmitPipeline(
        cfg, 420, GroundTruthWriter(ref_root / "gt"),
        SpillingEmitter(FakeProducer(), ref_root / "spill.jsonl", load_transport().emitter),
    )  # fmt: skip
    ref = Simulation.fresh(
        cfg, PostgresTms(ref_db), CheckpointStore(ref_root / "c.sqlite"), ref_pipeline
    )
    ref.run_until(end)
    ref.close()

    db = seeded(scratch_dbs.create("vlsim_early_kill"), cfg)
    first = _open(cfg, db, tmp_path, "early", load_transport())
    first.engine.run(until_ms=_start(cfg) + 40 * 60_000)  # 40 sim-min: no checkpoint yet
    first.flush_outputs()  # ground truth, topic and Postgres now hold early events ...
    _sink(first).producer.flush(10)
    assert first.pipeline is not None
    first.pipeline.gt.sync()  # ... and those ground-truth lines reached the disk
    first.engine.run(until_ms=_start(cfg) + 55 * 60_000)  # ... and 15 more sim-min stay unflushed
    tms = first.engine.ctx.tms
    assert isinstance(tms, PostgresTms) and tms.pending() > 0
    tms.discard_buffer()  # lost with the process: Postgres keeps only the partial, older rows
    run_dir = tmp_path / "gt" / "early"
    leftover_lines = sum(len(GroundTruthWriter(run_dir).read_lines(t)) for t in TOPICS)
    leftover_rows = _count(db, "shipments")
    assert leftover_lines > 0 and leftover_rows > 0, (leftover_lines, leftover_rows)
    assert leftover_rows < first.engine.state["seq"]["order"]  # partial: newest rows missing
    del first  # killed before any checkpoint

    again = _open(cfg, db, tmp_path, "early", load_transport())  # must not refuse the run dir
    assert again.engine.clock.now_ms == _start(cfg)
    assert sum(len(GroundTruthWriter(run_dir).read_lines(t)) for t in TOPICS) == 0  # reset
    again.run_until(end)
    again.close()

    for topic in TOPICS:
        ref_files = sorted(p.name for p in (ref_root / "gt" / topic).glob("*"))
        assert ref_files, topic
        assert ref_files == sorted(p.name for p in (run_dir / topic).glob("*"))
        for name in ref_files:
            assert (run_dir / topic / name).read_bytes() == (
                ref_root / "gt" / topic / name
            ).read_bytes()
    assert dump(db) == dump(ref_db)
    report = reconcile(run_dir, BOOTSTRAP, db)
    assert report["ok"], report
    assert report["topics"]["tracking.scans"]["duplicates_in_topic"] > 0


def _count(db: Any, table: str) -> int:
    with db.connect() as conn:
        n: int = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    return n
