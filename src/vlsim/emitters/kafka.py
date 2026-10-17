"""Batched Kafka/Redpanda producer with disk spillover and backpressure (hard rule 7, ADR 0010).

Delivery is at-least-once; consumers deduplicate on event_id. Rules:
- Each message is tracked until its delivery report arrives. A failed delivery (for example a
  broker outage hitting message.timeout.ms) appends the message to an on-disk spill file.
- Spilling is sticky: while the spill holds undrained messages, new messages are appended
  behind them instead of overtaking them. With more than `max_inflight` unacknowledged
  messages, new ones also spill, so memory stays bounded.
- `service()` re-sends spilled messages in batches when the broker keeps up. With more than
  `max_spill_bytes` waiting, it blocks inside librdkafka's poll (never the wall clock) until
  the backlog shrinks: the simulation slows down instead of crashing or dropping.
- `checkpoint()` waits briefly for acknowledgements, spills whatever is still unacknowledged,
  fsyncs, and returns the spill read cursor. So every message emitted before a checkpoint is
  either acknowledged by the broker or durable in the spill.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import structlog

from vlsim.config import EmitterTuning

log = structlog.get_logger()


class ProducerLike(Protocol):
    def produce(self, topic: str, key: bytes, value: bytes, on_delivery: Any) -> None: ...

    def poll(self, timeout: float = ...) -> int: ...

    def flush(self, timeout: float = ...) -> int: ...


class SpillingEmitter:
    def __init__(self, producer: ProducerLike, spill_path: Path, tuning: EmitterTuning) -> None:
        self.producer = producer
        self.tuning = tuning
        self.spill_path = spill_path
        spill_path.parent.mkdir(parents=True, exist_ok=True)
        self._append = spill_path.open("ab")
        self._read = spill_path.open("rb")
        self._cursor = 0
        self._inflight: dict[int, list[Any]] = {}  # id -> [topic, key, value, spilled]
        self._next_id = 0
        self.stats: Counter[str] = Counter()

    # --- public API ----------------------------------------------------------------------------
    def emit(self, topic: str, key: str, value: bytes) -> None:
        self.stats["emitted"] += 1
        if self.backlog_bytes() > 0 or len(self._inflight) >= self.tuning.max_inflight:
            self._spill(topic, key, value)
        else:
            self._produce(topic, key, value)

    def service(self) -> None:
        self.producer.poll(0)
        self._drain()
        while self.backlog_bytes() > self.tuning.max_spill_bytes:
            self.stats["backpressure_waits"] += 1
            if self.stats["backpressure_waits"] % 100 == 1:
                log.warning("emitter_backpressure", backlog_bytes=self.backlog_bytes())
            self.producer.poll(0.5)  # blocks inside librdkafka, not on our clock
            self._drain()

    def backlog_bytes(self) -> int:
        """Spilled bytes not yet re-sent."""
        if not self._append.closed:
            self._append.flush()
        return self.spill_path.stat().st_size - self._cursor

    def inflight(self) -> int:
        return len(self._inflight)

    def checkpoint(self) -> dict[str, int]:
        remaining = self.producer.flush(self.tuning.checkpoint_flush_timeout_s)
        if remaining:
            for entry in self._inflight.values():
                if not entry[3]:
                    self._write_spill_line(entry[0], entry[1], entry[2])
                    entry[3] = True  # a later failure report must not spill it again
                    self.stats["spilled"] += 1
        self._append.flush()
        os.fsync(self._append.fileno())
        if self.backlog_bytes() == 0 and not self._inflight:
            self._compact()
        return {"spill_cursor": self._cursor}

    def restore(self, state: dict[str, int]) -> None:
        size = self.spill_path.stat().st_size
        cursor = int(state.get("spill_cursor", 0))
        if cursor > size:
            raise RuntimeError(f"spill file shorter ({size}) than checkpointed cursor ({cursor})")
        self._cursor = cursor

    def close(self, max_rounds: int = 120) -> None:
        """Deliver everything possible; whatever the broker will not take stays in the spill."""
        for _ in range(max_rounds):
            self.service()
            self.producer.flush(1.0)
            if self.backlog_bytes() == 0 and not self._inflight:
                break
        self.checkpoint()
        self._append.close()
        self._read.close()

    # --- internals ----------------------------------------------------------------------------
    def _produce(self, topic: str, key: str, value: bytes) -> None:
        mid = self._next_id
        self._next_id += 1
        self._inflight[mid] = [topic, key, value, False]
        try:
            self.producer.produce(
                topic, key=key.encode(), value=value, on_delivery=partial(self._on_delivery, mid)
            )
        except BufferError:  # librdkafka's local queue is full: keep it on disk instead
            self._inflight.pop(mid)
            self._spill(topic, key, value)

    def _on_delivery(self, mid: int, err: Any, _msg: Any) -> None:
        entry = self._inflight.pop(mid, None)
        if entry is None:
            return
        if err is None:
            self.stats["delivered"] += 1
            return
        self.stats["delivery_failures"] += 1
        if not entry[3]:
            self._spill(entry[0], entry[1], entry[2])

    def _spill(self, topic: str, key: str, value: bytes) -> None:
        self._write_spill_line(topic, key, value)
        self.stats["spilled"] += 1

    def _write_spill_line(self, topic: str, key: str, value: bytes) -> None:
        line = json.dumps({"t": topic, "k": key, "v": value.decode("utf-8")}, ensure_ascii=False)
        self._append.write(line.encode("utf-8") + b"\n")

    def _drain(self) -> None:
        budget = self.tuning.drain_batch
        limit = self.tuning.max_inflight // 2
        if self.backlog_bytes() == 0:
            return
        self._read.seek(self._cursor)
        while budget > 0 and len(self._inflight) < limit:
            raw = self._read.readline()
            if not raw.endswith(b"\n"):
                break  # nothing complete left
            rec = json.loads(raw)
            self._cursor += len(raw)
            self._produce(rec["t"], rec["k"], rec["v"].encode("utf-8"))
            self.stats["drained"] += 1
            budget -= 1
            self.producer.poll(0)

    def _compact(self) -> None:
        self._append.close()
        self._read.close()
        self.spill_path.write_bytes(b"")
        self._append = self.spill_path.open("ab")
        self._read = self.spill_path.open("rb")
        self._cursor = 0
