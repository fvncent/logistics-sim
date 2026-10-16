"""A toy event chain exercising clock, queue ties, RNG streams, counters and state.

Orders arrive (exponential gaps), each schedules a pickup and a delivery; every tenth order
also schedules two same-instant events to exercise seq tie-breaking. Handlers append canonical
output lines to `ToyEngine.outputs`, which stands in for emitters and is deliberately NOT part of
the checkpoint (emitted output is already gone, like messages sent to a broker).
"""

from __future__ import annotations

from vlsim.engine.checkpoint import canonical_json
from vlsim.engine.core import Engine, HandlerRegistry
from vlsim.engine.events import Event

MINUTE_MS = 60_000


class ToyEngine(Engine):
    outputs: list[str]


def _emit(engine: Engine, kind: str, event: Event, **fields: object) -> None:
    assert isinstance(engine, ToyEngine)
    n = engine.counters.next(kind)
    engine.outputs.append(
        canonical_json(
            {
                "event_id": str(engine.counters.event_id(kind, n, fields)),
                "kind": kind,
                "event_time": engine.clock.isoformat(),
                "seq": event.seq,
                **fields,
            }
        )
    )


def on_order(engine: Engine, event: Event) -> None:
    st = engine.state
    st["orders"] = int(st.get("orders", 0)) + 1
    oid = f"O-{st['orders']:08d}"
    _emit(engine, "order", event, order_id=oid)
    pickup_ms = int(engine.rng("toy.pickup").integers(5, 90)) * MINUTE_MS
    engine.schedule_in(pickup_ms, "toy.pickup", {"order_id": oid})
    if st["orders"] % 10 == 0:
        for k in range(2):  # same instant: must run in scheduling order
            engine.schedule_in(MINUTE_MS, "toy.audit", {"order_id": oid, "k": k})
    gap_ms = int(engine.rng("toy.arrivals").exponential(7.0) * MINUTE_MS)
    engine.schedule_in(max(gap_ms, 1), "toy.order", {})


def on_pickup(engine: Engine, event: Event) -> None:
    _emit(engine, "pickup", event, order_id=event.payload["order_id"])
    travel_ms = int(engine.rng("toy.travel").integers(30, 600)) * MINUTE_MS
    engine.schedule_in(travel_ms, "toy.deliver", dict(event.payload))


def on_deliver(engine: Engine, event: Event) -> None:
    ok = bool(engine.rng("toy.outcome").random() < 0.9)
    engine.state["delivered" if ok else "failed"] = (
        int(engine.state.get("delivered" if ok else "failed", 0)) + 1
    )
    _emit(engine, "deliver", event, order_id=event.payload["order_id"], ok=ok)


def on_audit(engine: Engine, event: Event) -> None:
    _emit(engine, "audit", event, order_id=event.payload["order_id"], k=event.payload["k"])


def registry() -> HandlerRegistry:
    reg = HandlerRegistry()
    reg.register("toy.order", on_order)
    reg.register("toy.pickup", on_pickup)
    reg.register("toy.deliver", on_deliver)
    reg.register("toy.audit", on_audit)
    return reg
