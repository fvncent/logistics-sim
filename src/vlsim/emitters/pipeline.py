"""Release path for events: outbox -> ground truth -> fault injector -> producer.

Called only by Simulation.flush_outputs, after the TMS writes for the same events have
committed (ADR 0009). Ground truth records the true event before the injector can alter it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from vlsim.config import SimConfig
from vlsim.emitters.ground_truth import GroundTruthWriter
from vlsim.emitters.outbox import GpsPing, Outbox
from vlsim.engine.clock import ms_to_datetime
from vlsim.engine.ids import EmissionCounters
from vlsim.faults.injector import FaultInjector, PassThrough


class EventSink(Protocol):
    def emit(self, topic: str, key: str, value: bytes) -> None: ...

    def service(self) -> None: ...

    def checkpoint(self) -> dict[str, int]: ...

    def restore(self, state: dict[str, int]) -> None: ...

    def close(self) -> None: ...


def encode(event: dict[str, Any]) -> str:
    """Canonical JSON (sorted keys, compact, UTF-8). Events are already plain data."""
    return json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class EmitPipeline:
    def __init__(
        self,
        cfg: SimConfig,
        offset_minutes: int,
        ground_truth: GroundTruthWriter,
        sink: EventSink,
        injector: FaultInjector | None = None,
    ) -> None:
        self.cfg = cfg
        self.offset_minutes = offset_minutes
        self.gt = ground_truth
        self.sink = sink
        self.injector = injector or PassThrough()
        self.released = {"scans": 0, "gps": 0}

    def release(self, outbox: Outbox, until_ms: int) -> None:
        topics = self.cfg.emitters.topics
        scans, pings = outbox.release(until_ms)
        for key, event in scans:
            self._out(topics.scans, key, event)
        for p in pings:
            self._out(topics.gps, p.vehicle_id, self.gps_event(p))
        self.released["scans"] += len(scans)
        self.released["gps"] += len(pings)
        self.sink.service()

    def gps_event(self, p: GpsPing) -> dict[str, Any]:
        topic = self.cfg.emitters.topics.gps
        content = {
            "device_seq": p.seq,
            "event_time": ms_to_datetime(p.t_ms, self.offset_minutes).isoformat(
                timespec="milliseconds"
            ),
            "fuel_pct": p.fuel,
            "lat": p.lat,
            "lon": p.lon,
            "schema_v": self.cfg.emitters.schema_v,
            "speed_kmh": p.speed,
            "vehicle_id": p.vehicle_id,
        }
        # device_seq is the per-vehicle emission counter (checkpointed via EmissionCounters).
        event_id = EmissionCounters.event_id(f"{topic}:{p.vehicle_id}", p.seq, content)
        return {"event_id": str(event_id), **content}

    def _out(self, topic: str, key: str, event: dict[str, Any]) -> None:
        line = encode(event)
        self.gt.write(topic, event["event_time"][:10], line)  # truth first, before any fault
        for out_key, out_event in self.injector.process(topic, key, event):
            payload = line if out_event is event else encode(out_event)
            self.sink.emit(topic, out_key, payload.encode("utf-8"))

    def checkpoint(self, sim_now_ms: int) -> dict[str, Any]:
        """Seal completed days, fsync, and return the ground-truth watermark + spill cursor.
        Every event up to `sim_now_ms` has been released, so days before today are complete."""
        today = ms_to_datetime(sim_now_ms, self.offset_minutes).date().isoformat()
        self.gt.seal_before(today)
        return {"ground_truth": self.gt.watermark(), "sink": self.sink.checkpoint()}

    def restore(self, state: dict[str, Any]) -> None:
        self.gt.truncate_to(state["ground_truth"])
        self.sink.restore(state["sink"])

    def close(self) -> None:
        self.gt.sync()
        self.sink.close()
        self.gt.close()
