"""Simulation runner: builds or resumes a SimEngine and drives it in flush/checkpoint segments.

Order at every checkpoint boundary: run events up to the boundary -> flush TMS writes ->
save the checkpoint. The database is therefore never behind a saved checkpoint; it may be ahead
after a crash, which idempotent monotonic upserts absorb on replay (ADR 0008).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from vlsim.config import SimConfig
from vlsim.engine.checkpoint import CheckpointStore
from vlsim.engine.clock import MINUTE_MS, ms_from_datetime
from vlsim.models import flow
from vlsim.models.context import SimEngine, build_context, referenced_shipments
from vlsim.models.routes import _offset_minutes
from vlsim.ports import TmsService


class Simulation:
    def __init__(self, cfg: SimConfig, engine: SimEngine, store: CheckpointStore | None) -> None:
        self.cfg = cfg
        self.engine = engine
        self.store = store
        self.flush_ms = cfg.operations.runner.flush_interval_sim_minutes * MINUTE_MS
        self.checkpoint_ms = cfg.base.checkpoint.interval_sim_minutes * MINUTE_MS

    @classmethod
    def fresh(
        cls, cfg: SimConfig, tms: TmsService, store: CheckpointStore | None = None
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
        return cls(cfg, engine, store)

    @classmethod
    def resume(cls, cfg: SimConfig, tms: TmsService, store: CheckpointStore) -> Simulation | None:
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
        return cls(cfg, engine, store)

    @classmethod
    def open(cls, cfg: SimConfig, tms: TmsService, store: CheckpointStore) -> Simulation:
        return cls.resume(cfg, tms, store) or cls.fresh(cfg, tms, store)

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
            elif seg_end == next_flush:
                eng.ctx.tms.flush()
        eng.ctx.tms.flush()
        return processed

    def checkpoint(self) -> None:
        self.engine.ctx.tms.flush()
        if self.store is not None:
            self.store.save(self.engine.to_checkpoint())

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
