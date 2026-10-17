"""Simulation runner: builds or resumes a SimEngine and drives it in flush/checkpoint segments.

Order at every checkpoint boundary: run events up to the boundary -> flush TMS writes ->
save the checkpoint. The database is therefore never behind a saved checkpoint; it may be ahead
after a crash, which idempotent monotonic upserts absorb on replay (ADR 0008).

Flush cadence is mode-dependent (ADR 0009): fast runs flush on a sim-time grid
(`SimTimeFlush`); live mode flushes about every second of wall time
(`vlsim.engine.live.WallClockFlush`). A flush always commits Postgres first and only then
releases the matching events to the emitters, so topics never run ahead of the database.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Protocol

from vlsim.config import SimConfig
from vlsim.emitters.pipeline import EmitPipeline
from vlsim.engine.checkpoint import CheckpointStore
from vlsim.engine.clock import MINUTE_MS, ms_from_datetime
from vlsim.models import flow
from vlsim.models.context import SimEngine, build_context, referenced_shipments
from vlsim.models.routes import _offset_minutes
from vlsim.ports import TmsService


class FlushPolicy(Protocol):
    def due(self, sim_now_ms: int) -> bool: ...

    def flushed(self, sim_now_ms: int) -> None: ...


class SimTimeFlush:
    """Fast runs: flush whenever sim time crosses the next multiple of `interval_ms`."""

    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self._last_slot: int | None = None

    def due(self, sim_now_ms: int) -> bool:
        return self._last_slot is None or sim_now_ms // self.interval_ms > self._last_slot

    def flushed(self, sim_now_ms: int) -> None:
        self._last_slot = sim_now_ms // self.interval_ms


class Simulation:
    def __init__(
        self,
        cfg: SimConfig,
        engine: SimEngine,
        store: CheckpointStore | None,
        flush_policy: FlushPolicy | None = None,
        pipeline: EmitPipeline | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.store = store
        self.pipeline = pipeline
        self.flush_ms = cfg.operations.runner.flush_interval_sim_minutes * MINUTE_MS
        self.checkpoint_ms = cfg.base.checkpoint.interval_sim_minutes * MINUTE_MS
        self.flush_policy: FlushPolicy = flush_policy or SimTimeFlush(self.flush_ms)
        self._checkpoint_pending = False
        self.checkpoints_saved = 0
        self.last_checkpoint_ms = engine.clock.now_ms
        self.last_flush_ms = engine.clock.now_ms

    @classmethod
    def fresh(
        cls,
        cfg: SimConfig,
        tms: TmsService,
        store: CheckpointStore | None = None,
        pipeline: EmitPipeline | None = None,
    ) -> Simulation:
        engine = SimEngine(
            seed=cfg.base.seed,
            start_ms=ms_from_datetime(cfg.base.start_time),
            offset_minutes=_offset_minutes(cfg.base.timezone_offset),
            registry=flow.registry(),
            fingerprint=cfg.fingerprint(),
        )
        engine.ctx = build_context(cfg, tms)
        flow.bootstrap(engine)
        return cls(cfg, engine, store, pipeline=pipeline)

    @classmethod
    def resume(
        cls,
        cfg: SimConfig,
        tms: TmsService,
        store: CheckpointStore,
        pipeline: EmitPipeline | None = None,
    ) -> Simulation | None:
        """Restore from the latest checkpoint (refused if the config changed), or None."""
        data = store.load_latest()
        if data is None:
            return None
        engine = SimEngine.from_checkpoint(data, flow.registry(), fingerprint=cfg.fingerprint())
        engine.ctx = build_context(cfg, tms)
        facts = tms.load_shipment_facts(referenced_shipments(data))
        engine.ctx.facts = {f.shipment_id: f for f in facts}
        missing = set(referenced_shipments(data)) - set(engine.ctx.facts)
        if missing:
            raise RuntimeError(
                f"checkpoint references shipments missing from TMS: {sorted(missing)[:5]}"
            )
        if pipeline is not None:
            if "emitters" not in data:
                raise RuntimeError("checkpoint was taken without emitters; cannot resume emitting")
            # Cut ground truth back to the checkpoint and resume the spill where it was.
            pipeline.restore(data["emitters"])
        # Pings after the checkpoint instant were never released: regenerate them per trip.
        now = engine.clock.now_ms
        for tid in sorted(engine.state["trips"]):
            trip = engine.state["trips"][tid]
            if "gps" in trip:
                engine.ctx.outbox.add_gps(flow.gps_batch(engine, tid, trip), after_ms=now)
        return cls(cfg, engine, store, pipeline=pipeline)

    @classmethod
    def open(
        cls,
        cfg: SimConfig,
        tms: TmsService,
        store: CheckpointStore,
        pipeline: EmitPipeline | None = None,
    ) -> Simulation:
        return cls.resume(cfg, tms, store, pipeline) or cls.fresh(cfg, tms, store, pipeline)

    def run_until(self, end_ms: int) -> int:
        """Advance to end_ms; flush on the flush grid and checkpoint on the checkpoint grid
        (grids are absolute multiples, so a resumed run hits the same boundaries)."""
        eng, processed = self.engine, 0
        while eng.clock.now_ms < end_ms:
            now = eng.clock.now_ms
            next_flush = (now // self.flush_ms + 1) * self.flush_ms
            next_ckpt = (now // self.checkpoint_ms + 1) * self.checkpoint_ms
            seg_end = min(next_flush, next_ckpt, end_ms)
            processed += eng.run(until_ms=seg_end)
            if seg_end == next_ckpt:
                self.checkpoint()
            else:
                self.flush_if_due()
        self.flush_outputs()
        return processed

    def flush_outputs(self) -> None:
        """Commit TMS writes, then release the events they correspond to (ADR 0009). If the
        commit raises, nothing is released."""
        now = self.engine.clock.now_ms
        self.engine.ctx.tms.flush()
        if self.pipeline is not None:
            self.pipeline.release(self.engine.ctx.outbox, now)
        else:
            self.engine.ctx.outbox.release(now)  # no emitters configured: discard
        self.flush_policy.flushed(now)
        self.last_flush_ms = now

    def flush_if_due(self) -> bool:
        if self.flush_policy.due(self.engine.clock.now_ms):
            self.flush_outputs()
            return True
        return False

    def checkpoint(self) -> None:
        """Flush (DB, then events), make emitted output durable, then save the checkpoint."""
        self.flush_outputs()
        if self.store is None:
            return
        now = self.engine.clock.now_ms
        emitters = self.pipeline.checkpoint(now) if self.pipeline is not None else None
        data = self.engine.to_checkpoint()
        if emitters is not None:
            data["emitters"] = emitters  # ground-truth watermark + spill cursor
        self.store.save(data)
        self.checkpoints_saved += 1
        self.last_checkpoint_ms = now

    # --- live mode (driven by vlsim.engine.live.LiveRunner) ------------------------------------
    def now_ms(self) -> int:
        return self.engine.clock.now_ms

    def next_stop_ms(self) -> int | None:
        """Next instant the runner must reach: the next event or the next checkpoint boundary."""
        now = self.engine.clock.now_ms
        boundary = (now // self.checkpoint_ms + 1) * self.checkpoint_ms
        nxt = self.engine.queue.peek_time()
        return boundary if nxt is None else min(nxt, boundary)

    def advance(self, until_ms: int) -> int:
        """Process events up to until_ms (never past a checkpoint boundary); no I/O here."""
        boundary = (self.engine.clock.now_ms // self.checkpoint_ms + 1) * self.checkpoint_ms
        processed = self.engine.run(until_ms=min(until_ms, boundary))
        if self.engine.clock.now_ms == boundary:
            self._checkpoint_pending = True
        return processed

    def housekeeping(self) -> None:
        """Checkpoint if a boundary was reached, else flush if the policy says so. Safe to
        retry after a transient failure: nothing is released before the TMS commit."""
        if self._checkpoint_pending:
            self.checkpoint()
            self._checkpoint_pending = False
        else:
            self.flush_if_due()

    def close(self) -> None:
        """Graceful stop: final checkpoint, then deliver everything the broker will take."""
        self.checkpoint()
        if self.pipeline is not None:
            self.pipeline.close()

    def in_flight(self) -> dict[str, str]:
        """shipment_id -> where the engine holds it (queue or trip), for conservation checks."""
        st = self.engine.state
        where: dict[str, str] = {}
        for dep in sorted(st["depots"]):
            q = st["depots"][dep]
            places = [("pickup", q["pickup"]), ("lastmile", q["lastmile"])]
            places += [(f"linehaul:{hop}", q["linehaul"][hop]) for hop in sorted(q["linehaul"])]
            places += [
                ("dock-wait", it) for job in st["docks"][dep]["waiting"] for it in [job["items"]]
            ]
            for place, items in places:
                where.update({it[0]: f"{dep}:{place}" for it in items})
        for tid in sorted(st["trips"]):
            t = st["trips"][tid]
            pending = (
                t.get("items", []) + t.get("stops", [])[t.get("next", 0) :] + t.get("failed", [])
            )
            where.update({it[0]: f"trip:{tid}" for it in pending})
        queued = [e.to_data() for e in self.engine.queue.snapshot()]
        for sid in referenced_shipments({"state": {}, "queue": queued}):
            where.setdefault(sid, "event")
        return where

    def summary(self) -> dict[str, Any]:
        eng = self.engine
        return {
            "sim_time": eng.clock.isoformat(),
            "events_processed": eng.events_processed,
            "orders_created": eng.state["seq"]["order"],
            "trips": eng.state["seq"]["trip"],
            "in_flight": len(self.in_flight()),
            "in_flight_by_place": dict(
                sorted(
                    Counter(
                        v.split(":")[1] if ":" in v else v for v in self.in_flight().values()
                    ).items()
                )
            ),
        }


def _place(where: str) -> str:
    """'DP-HAN:linehaul:DP-DAD' -> 'linehaul'; 'trip:T-1' -> 'trip'; 'event' -> 'event'."""
    parts = where.split(":")
    return parts[0] if parts[0] in ("trip", "event") else parts[1]
