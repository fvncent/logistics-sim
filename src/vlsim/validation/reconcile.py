"""Reconcile one run: ground truth vs Redpanda (distinct event_ids, scoped by the run's start
offsets) and vs TMS Postgres (each shipment's last scanned status equals its current_status).

Run it after a clean stop (`Simulation.close`): Postgres commits before events are emitted, so
mid-run the topics may legitimately lag the database by one flush interval.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from confluent_kafka import Consumer, TopicPartition
from sqlalchemy import Engine, text

from vlsim.emitters.run import load_run


def ground_truth_ids(run_dir: Path, topic: str) -> tuple[list[str], dict[str, str]]:
    """All event_ids in file order, and (for scans) the last event_type per shipment."""
    ids: list[str] = []
    last_status: dict[str, str] = {}
    for f in sorted((run_dir / topic).glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                ev = json.loads(line)
                ids.append(ev["event_id"])
                if "shipment_id" in ev:
                    last_status[ev["shipment_id"]] = ev["event_type"]
    return ids, last_status


def topic_ids(bootstrap: str, topic: str, start: dict[str, int]) -> tuple[set[str], int]:
    """Distinct event_ids and total message count from `start` offsets to the current end."""
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": "vlsim-reconcile",
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    try:
        parts = sorted(int(p) for p in start)
        ends = {
            p: consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=10)[1]
            for p in parts
        }
        todo = {p for p in parts if ends[p] > start[str(p)]}
        consumer.assign([TopicPartition(topic, p, start[str(p)]) for p in parts])
        ids: set[str] = set()
        total = 0
        while todo:
            for msg in consumer.consume(num_messages=10_000, timeout=5.0):
                if msg.error():
                    raise RuntimeError(str(msg.error()))
                p, off, value = msg.partition(), msg.offset(), msg.value()
                if p is None or off is None or value is None or p not in todo or off >= ends[p]:
                    continue
                ids.add(json.loads(value)["event_id"])
                total += 1
                if off + 1 >= ends[p]:
                    todo.discard(p)
        return ids, total
    finally:
        consumer.close()


def reconcile(run_dir: Path, bootstrap: str, db: Engine) -> dict[str, Any]:
    manifest = load_run(run_dir)
    report: dict[str, Any] = {"run_id": manifest["run_id"], "topics": {}, "postgres": {}}
    ok = True
    last_status: dict[str, str] = {}
    for topic in sorted(manifest["start_offsets"]):
        gt_list, statuses = ground_truth_ids(run_dir, topic)
        last_status.update(statuses)
        gt = set(gt_list)
        got, total = topic_ids(bootstrap, topic, manifest["start_offsets"][topic])
        missing, extra = gt - got, got - gt
        topic_ok = not missing and not extra and len(gt) == len(gt_list)
        ok &= topic_ok
        report["topics"][topic] = {
            "ground_truth_events": len(gt_list),
            "ground_truth_distinct": len(gt),
            "topic_messages": total,
            "topic_distinct": len(got),
            "duplicates_in_topic": total - len(got),
            "missing_from_topic": len(missing),
            "unexpected_in_topic": len(extra),
            "sample_missing": sorted(missing)[:5],
            "ok": topic_ok,
        }
    with db.connect() as conn:
        rows = conn.execute(text("SELECT shipment_id, current_status FROM shipments")).all()
    db_status: dict[str, str] = {r[0]: r[1] for r in rows}
    mismatched = sorted(
        s for s in db_status.keys() | last_status.keys() if db_status.get(s) != last_status.get(s)
    )
    pg_ok = not mismatched
    ok &= pg_ok
    report["postgres"] = {
        "shipments_in_db": len(db_status),
        "shipments_in_ground_truth": len(last_status),
        "status_counts_db": dict(sorted(Counter(db_status.values()).items())),
        "status_counts_ground_truth": dict(sorted(Counter(last_status.values()).items())),
        "status_mismatches": len(mismatched),
        "sample_mismatches": mismatched[:5],
        "ok": pg_ok,
    }
    report["ok"] = ok
    return report
