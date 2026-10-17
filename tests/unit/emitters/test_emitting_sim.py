"""The emitting simulation end to end with in-memory TMS and broker fakes."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import InMemoryTms
from tests.unit.emitters.fake_broker import FakeProducer
from vlsim.config import SimConfig, load_config, load_transport
from vlsim.emitters.ground_truth import GroundTruthWriter
from vlsim.emitters.kafka import SpillingEmitter
from vlsim.emitters.pipeline import EmitPipeline
from vlsim.engine.checkpoint import CheckpointStore, checkpoint_hash
from vlsim.engine.clock import DAY_MS, HOUR_MS, ms_from_datetime
from vlsim.models.sim import Simulation

SCANS, GPS = "tracking.scans", "telemetry.gps"
REASONS = {
    "damage",
    "lost",
    "address_unresolvable",
    "recipient_unavailable",
    "address_issue",
    "cod_refused",
    "max_attempts",
}


@pytest.fixture(scope="module")
def cfg() -> SimConfig:
    return load_config(preset="s")


def _pipeline(cfg: SimConfig, root: Path, producer: FakeProducer) -> EmitPipeline:
    sink = SpillingEmitter(producer, root / "spill.jsonl", load_transport().emitter)
    return EmitPipeline(cfg, 420, GroundTruthWriter(root / "gt"), sink)


class GuardedProducer(FakeProducer):
    """Fails the test if a scan reaches the broker before its DB row is committed, or a ping
    arrives from the future."""

    def __init__(self, tms: InMemoryTms) -> None:
        super().__init__()
        self.tms = tms
        self.sim: Simulation | None = None
        self.violations: list[str] = []

    def produce(self, topic: str, key: bytes, value: bytes, on_delivery: Any) -> None:
        ev = json.loads(value)
        t = ms_from_datetime(datetime.fromisoformat(ev["event_time"]))
        if topic == SCANS:
            row = self.tms.tables["shipments"].get(ev["shipment_id"])
            if row is None or ms_from_datetime(row.updated_at) < t:
                self.violations.append(
                    f"scan {ev['event_type']} for {ev['shipment_id']} before commit"
                )
        if self.sim is not None and t > self.sim.engine.clock.now_ms:
            self.violations.append(f"{topic} event from the future at {ev['event_time']}")
        super().produce(topic, key, value, on_delivery)


@pytest.fixture(scope="module")
def run(cfg: SimConfig, tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("emit")
    tms = InMemoryTms(cfg)
    producer = GuardedProducer(tms)
    sim = Simulation.fresh(
        cfg, tms, CheckpointStore(root / "ckpt.sqlite"), _pipeline(cfg, root, producer)
    )
    producer.sim = sim
    sim.run_until(ms_from_datetime(cfg.base.start_time) + 2 * DAY_MS)
    sim.close()
    return sim, tms, producer, root


def test_db_commits_before_every_scan_and_no_future_events(run: Any) -> None:
    _sim, _tms, producer, _root = run
    assert producer.violations == []


def test_one_scan_per_status_write_with_reason_codes(run: Any) -> None:
    _sim, tms, _p, root = run
    scans = [json.loads(x) for x in GroundTruthWriter(root / "gt").read_lines(SCANS)]
    assert [(e["shipment_id"], e["event_type"]) for e in scans] == [
        (s, st) for s, st, _ in tms.history
    ]
    for e in scans:
        assert e["schema_v"] == 1 and e["event_time"].endswith("+07:00")
        if e["event_type"] in ("EXCEPTION", "DELIVERY_FAILED", "RETURNED"):
            assert e["reason_code"] in REASONS
        else:
            assert e["reason_code"] is None
    assert len({e["event_id"] for e in scans}) == len(scans)


def test_gps_device_seq_contiguous_per_vehicle(run: Any) -> None:
    _sim, _tms, _p, root = run
    seqs: dict[str, list[int]] = defaultdict(list)
    for line in GroundTruthWriter(root / "gt").read_lines(GPS):
        e = json.loads(line)
        seqs[e["vehicle_id"]].append(e["device_seq"])
    assert len(seqs) > 30
    for vid, s in seqs.items():
        assert s == sorted(s) and s[0] == 1, vid
        assert len(set(s)) == len(s), vid


def test_everything_in_ground_truth_reached_the_broker(run: Any) -> None:
    _sim, _tms, producer, root = run
    gt = GroundTruthWriter(root / "gt")
    truth = {json.loads(x)["event_id"] for t in (SCANS, GPS) for x in gt.read_lines(t)}
    delivered = {json.loads(v)["event_id"] for _t, _k, v in producer.log}
    assert truth == delivered


def test_hard_crash_resume_reproduces_ground_truth_exactly(cfg: SimConfig, tmp_path: Path) -> None:
    end = ms_from_datetime(cfg.base.start_time) + 2 * DAY_MS
    ref_root, crash_root = tmp_path / "ref", tmp_path / "crash"
    ref = Simulation.fresh(cfg, InMemoryTms(cfg), CheckpointStore(ref_root / "c.sqlite"),
                           _pipeline(cfg, ref_root, FakeProducer()))  # fmt: skip
    ref.run_until(end)
    ref.close()

    tms, broker = InMemoryTms(cfg), FakeProducer()
    store = CheckpointStore(crash_root / "c.sqlite")
    first = Simulation.fresh(cfg, tms, store, _pipeline(cfg, crash_root, broker))
    first.run_until(ms_from_datetime(cfg.base.start_time) + DAY_MS)  # checkpoint at 24 h
    first.engine.run(until_ms=first.engine.clock.now_ms + 5 * HOUR_MS)
    first.flush_outputs()  # post-checkpoint events reach ground truth and the broker ...
    broker.poll()
    first.engine.run(until_ms=first.engine.clock.now_ms + HOUR_MS)
    tms.crash()  # ... then the process dies (TMS buffer, outbox, producer queue all lost)
    del first

    resumed = Simulation.resume(cfg, tms, store, _pipeline(cfg, crash_root, broker))
    assert resumed is not None
    resumed.run_until(end)
    resumed.close()

    for topic in (SCANS, GPS):
        for f in sorted((ref_root / "gt" / topic).glob("*.jsonl")):
            assert (crash_root / "gt" / topic / f.name).read_bytes() == f.read_bytes(), f.name
    assert checkpoint_hash(resumed.engine.to_checkpoint()) == checkpoint_hash(
        ref.engine.to_checkpoint()
    )
    gt = GroundTruthWriter(crash_root / "gt")
    truth = {json.loads(x)["event_id"] for t in (SCANS, GPS) for x in gt.read_lines(t)}
    delivered = [json.loads(v)["event_id"] for _t, _k, v in broker.log]
    assert set(delivered) == truth  # nothing lost ...
    assert len(delivered) > len(truth)  # ... and the replayed hours arrived twice (deduped by id)
