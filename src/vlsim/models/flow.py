"""Business handlers: demand, pickup, dock queues, line-haul, last mile.

Checkpointed state layout (plain data, see ADR 0008):
  seq       {"order": n, "trip": n, "cod": n}                      id counters
  vehicles  {vehicle_id: {"at": depot|None, "busy": bool}}          positions
  drivers   {driver_id: {"at", "busy", "free_ms", "shift_end_ms"}}  positions / availability
  docks     {depot: {"doors", "busy", "waiting": [job]}}            FIFO dock doors
  depots    {depot: {"pickup": [item], "lastmile": [item], "linehaul": {next_hop: [item]}}}
  trips     {trip_id: {...in-flight trip incl. its items...}}
An item is [shipment_id, ready_ms, attempts]. Dicts are only ever iterated in sorted order.
"""

from __future__ import annotations

import math
from typing import Any, cast

from vlsim.config import LaneKind, VehicleType
from vlsim.emitters.gps import PingBatch, generate_pings, ping_count
from vlsim.engine.clock import DAY_MS, HOUR_MS, MINUTE_MS, SECOND_MS, next_local_time_ms
from vlsim.engine.core import Engine, HandlerRegistry
from vlsim.engine.events import Event
from vlsim.engine.rng import stream
from vlsim.models.context import SimEngine, as_sim
from vlsim.models.demand import draw_cod_vnd, draw_weight_g
from vlsim.models.dock import DockQueue
from vlsim.models.statemachine import ORDER_STATUS_FOR
from vlsim.ports import CodCollectionRow, ShipmentFacts, TripRow, TripStopRow

Item = list[Any]  # [shipment_id, ready_ms, attempts]


# --- shared helpers --------------------------------------------------------------------------
def transition(
    sim: SimEngine,
    sid: str,
    status: str,
    attempts: int,
    *,
    depot: str,
    delivered: bool = False,
    device: str | None = None,
    driver: str | None = None,
    reason: str | None = None,
) -> None:
    """Write a shipment status (and the order's, when it changes) at the current sim time, and
    queue the matching scan. The scan leaves only after this write commits (ADR 0009)."""
    sim.write_shipment(sid, status, attempts, delivered=delivered)
    order_status = ORDER_STATUS_FOR.get(status)
    if order_status is not None:
        sim.write_order(sid, order_status)
    sim.emit_scan(sid, status, depot=depot, device=device, driver=driver, reason=reason)
    if status in ("DELIVERED", "RETURNED", "EXCEPTION"):
        sim.ctx.facts.pop(sid)  # terminal: no longer needed in the in-flight cache


def _depot(sim: SimEngine, dep: str) -> dict[str, Any]:
    d: dict[str, Any] = sim.state["depots"][dep]
    return d


def _route_ready(sim: SimEngine, dep: str, item: Item) -> None:
    """A sorted parcel at `dep` joins the last-mile queue or the queue for its next hop."""
    f = sim.ctx.facts[item[0]]
    if f.dest_depot == dep:
        _depot(sim, dep)["lastmile"].append(item)
    else:
        hop = sim.ctx.routes.next_hop(dep, f.dest_depot)
        _depot(sim, dep)["linehaul"].setdefault(hop, []).append(item)


def _rng_hit(sim: SimEngine, stream: str, p: float) -> bool:
    return bool(sim.rng(stream).random() < p)


def _daily(sim: SimEngine, handler: str, payload: dict[str, Any]) -> None:
    sim.schedule_in(DAY_MS, handler, payload)


# --- bootstrap ---------------------------------------------------------------------------------
def bootstrap(sim: SimEngine) -> None:
    """Initial state and recurring events for a brand-new run (not used on restore)."""
    ctx, cfg, now = sim.ctx, sim.ctx.cfg, sim.clock.now_ms
    off = sim.clock.offset_minutes
    sim.state.update(
        {
            "seq": {"order": 0, "trip": 0, "cod": 0},
            "vehicles": {
                v: {"at": ctx.vehicles[v].home_depot, "busy": False} for v in sorted(ctx.vehicles)
            },
            "drivers": {
                d: {
                    "at": ctx.drivers[d].home_depot,
                    "busy": False,
                    "free_ms": now,
                    "shift_end_ms": now,
                }
                for d in sorted(ctx.drivers)
            },
            "docks": {f"DP-{d.code}": DockQueue.new(d.dock_doors).data for d in cfg.network.depots},
            "depots": {
                f"DP-{d.code}": {"pickup": [], "lastmile": [], "linehaul": {}}
                for d in cfg.network.depots
            },
            "trips": {},
        }
    )
    sim.schedule(now, "demand.tick", {"candidate": False})
    for dep in ctx.routes.depot_ids:
        for t in cfg.operations.pickup.runs:
            sim.schedule(
                next_local_time_ms(now, t.hour, t.minute, off), "pickup.run", {"depot": dep}
            )
    lh = cfg.operations.linehaul
    for a, b, kind in ctx.routes.directed_lanes():
        slots = sorted(lh.departures[kind])
        for i, t in enumerate(slots):
            sim.schedule(
                next_local_time_ms(now, t.hour, t.minute, off),
                "linehaul.slot",
                {"from": a, "to": b, "last": i == len(slots) - 1},
            )
    for rider in sorted(ctx.rider_bike):
        s = ctx.drivers[rider].shift_start
        sim.schedule(
            next_local_time_ms(now, s.hour, s.minute, off), "rider.shift", {"rider": rider}
        )


# --- demand ------------------------------------------------------------------------------------
def on_demand_tick(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    dm, now = sim.ctx.demand, sim.clock.now_ms
    bound = dm.day_bound_per_ms(now)
    if event.payload["candidate"] and sim.rng("demand.accept").random() * bound < dm.rate_per_ms(
        now
    ):
        _create_order(sim)
    gap = max(1, math.ceil(float(sim.rng("demand.gap").exponential(1.0 / bound))))
    midnight = dm.next_local_midnight(now)
    if now + gap >= midnight:  # restart at midnight with the next day's bound (memoryless)
        sim.schedule(midnight, "demand.tick", {"candidate": False})
    else:
        sim.schedule(now + gap, "demand.tick", {"candidate": True})


def _create_order(sim: SimEngine) -> None:
    ctx, cfg, now = sim.ctx, sim.ctx.cfg, sim.clock.now_ms
    merchant = ctx.merchant_ids[
        int(sim.rng("demand.merchant").choice(len(ctx.merchant_ids), p=ctx.merchant_p))
    ]
    customer = ctx.customer_ids[int(sim.rng("demand.customer").integers(len(ctx.customer_ids)))]
    addrs = ctx.addresses_by_customer[customer]
    addr = addrs[int(sim.rng("demand.address").integers(len(addrs)))]
    origin, dest = ctx.merchant_depot[merchant], ctx.address_depot[addr.address_id]
    cls = ctx.routes.distance_class(origin, dest)
    express = _rng_hit(sim, "demand.service", cfg.demand.express_share[cls])
    weight_g = draw_weight_g(sim.rng("demand.weight"), cfg.demand.weight_kg)
    cod = 0
    if _rng_hit(sim, "demand.cod", ctx.merchant_cod_share[merchant]):
        cod = draw_cod_vnd(sim.rng("demand.cod_value"), cfg.demand.cod_value_vnd)
    n = sim.next_id("order")
    sid = f"S-{n:08d}"
    ctx.facts[sid] = ShipmentFacts(
        shipment_id=sid,
        order_id=f"O-{n:08d}",
        merchant_id=merchant,
        customer_id=customer,
        address_id=addr.address_id,
        origin_depot=origin,
        dest_depot=dest,
        service_level="express" if express else "standard",
        weight_g=weight_g,
        cod_vnd=cod,
        created_ms=now,
        promised_ms=ctx.routes.promised_ms(now, cls, express=express),
        carrier_id=cfg.operations.carrier_id,
        lat=addr.lat,
        lon=addr.lon,
        quality=addr.quality,
    )
    sim.write_order(sid, "CREATED")  # order before shipment (foreign key)
    sim.write_shipment(sid, "CREATED", 0)
    sim.emit_scan(sid, "CREATED", depot=origin, device=cfg.emitters.devices.merchant_api)
    _depot(sim, origin)["pickup"].append([sid, now, 0])


# --- pickup and docks ----------------------------------------------------------------------------
def on_pickup_run(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    dep = event.payload["depot"]
    _daily(sim, "pickup.run", {"depot": dep})
    now = sim.clock.now_ms
    queue = _depot(sim, dep)["pickup"]
    # Strictly earlier: a shipment created at this very millisecond waits for the next run, so
    # no shipment ever gets two status writes at the same sim time (ADR 0008).
    items = [it for it in queue if it[1] < now]
    if not items:
        return
    _depot(sim, dep)["pickup"] = [it for it in queue if it[1] >= now]
    device = sim.device("pickup_handheld", dep)
    for item in items:
        transition(sim, item[0], "PICKED_UP", item[2], depot=dep, device=device)
    travel = sim.ctx.cfg.operations.pickup.travel_minutes * MINUTE_MS
    job = {"kind": "pickup", "trip_id": None, "vehicle_id": None, "items": items}
    sim.schedule_in(travel, "dock.arrive", {"depot": dep, "job": job})


def on_dock_arrive(engine: Engine, event: Event) -> None:
    _dock_arrive(as_sim(engine), event.payload["depot"], event.payload["job"])


def _dock_arrive(sim: SimEngine, dep: str, job: dict[str, Any]) -> None:
    if DockQueue(sim.state["docks"][dep]).arrive(job):
        _start_unload(sim, dep, job)


def _start_unload(sim: SimEngine, dep: str, job: dict[str, Any]) -> None:
    dock = sim.ctx.cfg.operations.dock
    dur = dock.unload_base_minutes * MINUTE_MS + dock.unload_seconds_per_parcel * SECOND_MS * len(
        job["items"]
    )
    sim.schedule_in(max(dur, 1), "dock.done", {"depot": dep, "job": job})


def on_dock_done(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    ops = sim.ctx.cfg.operations
    dep, job = event.payload["depot"], event.payload["job"]
    ready = sim.clock.now_ms + ops.dock.sort_minutes * MINUTE_MS
    same_depot: list[Item] = []
    for sid, _ready, attempts in job["items"]:
        if _rng_hit(sim, "ops.damage", ops.exceptions.damage_per_unload):
            transition(sim, sid, "EXCEPTION", attempts, depot=dep, reason="damage")
            continue
        f = sim.ctx.facts[sid]
        if job["kind"] == "pickup":
            transition(sim, sid, "AT_ORIGIN_DEPOT", attempts, depot=dep)
            if f.dest_depot == dep:
                same_depot.append([sid, ready, attempts])
                continue
        else:
            status = "AT_DEST_DEPOT" if f.dest_depot == dep else "AT_HUB"
            transition(sim, sid, status, attempts, depot=dep)
        _route_ready(sim, dep, [sid, ready, attempts])
    if same_depot:
        sim.schedule(ready, "sort.local", {"depot": dep, "items": same_depot})
    if job["vehicle_id"] is not None:
        sim.state["vehicles"][job["vehicle_id"]]["busy"] = False
    nxt = DockQueue(sim.state["docks"][dep]).release()
    if nxt is not None:
        _start_unload(sim, dep, nxt)


def on_sort_local(engine: Engine, event: Event) -> None:
    """Same-depot parcels: sorted straight into the depot's own last-mile queue."""
    sim = as_sim(engine)
    dep = event.payload["depot"]
    for sid, ready, attempts in event.payload["items"]:
        transition(sim, sid, "AT_DEST_DEPOT", attempts, depot=dep)
        _depot(sim, dep)["lastmile"].append([sid, ready, attempts])


# --- line-haul -----------------------------------------------------------------------------------
def on_linehaul_slot(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    p = event.payload
    _daily(sim, "linehaul.slot", dict(p))
    now = sim.clock.now_ms
    wait = sim.ctx.cfg.operations.linehaul.max_wait_minutes * MINUTE_MS
    _try_depart(sim, p["from"], p["to"], slot_ms=now, last=p["last"], deadline=now + wait)


def on_linehaul_retry(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    p = event.payload
    _try_depart(
        sim, p["from"], p["to"], slot_ms=p["slot_ms"], last=p["last"], deadline=p["deadline"]
    )


def _pick_vehicle(sim: SimEngine, at: str, to: str, kind: LaneKind) -> str | None:
    allowed = list(sim.ctx.cfg.operations.linehaul.vehicle_types[kind])
    best: tuple[Any, ...] | None = None
    for vid in sorted(sim.state["vehicles"]):
        st, v = sim.state["vehicles"][vid], sim.ctx.vehicles[vid]
        if st["busy"] or st["at"] != at or v.type not in allowed:
            continue
        if v.home_depot not in (at, to):
            continue  # away from home: only legs that bring the vehicle back
        key = (v.home_depot != to, allowed.index(v.type), -v.capacity_kg, vid)
        if best is None or key < best:
            best = key
    return None if best is None else str(best[-1])


def _pick_driver(sim: SimEngine, at: str, to: str) -> str | None:
    now, best = sim.clock.now_ms, None
    for did in sorted(sim.ctx.crew):
        st = sim.state["drivers"][did]
        if st["busy"] or st["at"] != at or st["free_ms"] > now:
            continue
        home = sim.ctx.drivers[did].home_depot
        if home not in (at, to):
            continue  # away from home: only legs that bring the driver back
        key = (sim.ctx.drivers[did].home_depot != to, did)
        if best is None or key < best:
            best = key
    return None if best is None else best[1]


def _try_depart(sim: SimEngine, a: str, b: str, *, slot_ms: int, last: bool, deadline: int) -> None:
    ctx, lh, now = sim.ctx, sim.ctx.cfg.operations.linehaul, sim.clock.now_ms
    kind = ctx.routes.lane_kind[(a, b)]
    queue: list[Item] = _depot(sim, a)["linehaul"].get(b, [])
    cutoff = slot_ms - lh.cutoff_minutes * MINUTE_MS
    eligible = sorted((it for it in queue if it[1] < cutoff), key=lambda it: (it[1], it[0]))
    vid, did = _pick_vehicle(sim, a, b, kind), _pick_driver(sim, a, b)
    repositioning = vid is not None and ctx.vehicles[vid].home_depot == b
    if not eligible and not repositioning:
        return
    if vid is None or did is None:
        if now + lh.retry_minutes * MINUTE_MS <= deadline:
            sim.schedule_in(
                lh.retry_minutes * MINUTE_MS,
                "linehaul.retry",
                {"from": a, "to": b, "slot_ms": slot_ms, "last": last, "deadline": deadline},
            )
        return
    cap_g, load_g, loaded = ctx.vehicles[vid].capacity_kg * 1000, 0, []
    for it in eligible:
        w = ctx.facts[it[0]].weight_g
        if load_g + w > cap_g:
            break
        load_g += w
        loaded.append(it)
    if not last and not repositioning and load_g < lh.min_fill_ratio * cap_g:
        return  # consolidate into a later departure (truck fill)
    taken = {it[0] for it in loaded}
    _depot(sim, a)["linehaul"][b] = [it for it in queue if it[0] not in taken]

    v = ctx.vehicles[vid]
    drive_ms = ctx.routes.distance_km(a, b) / lh.speed_kmh[cast(VehicleType, v.type)] * HOUR_MS
    handling = lh.handling_minutes * MINUTE_MS
    planned = int(drive_ms) + handling
    noise = math.exp(lh.travel_noise_sigma * float(sim.rng("linehaul.travel").standard_normal()))
    actual = max(1, int(drive_ms * noise) + handling)
    tid = f"T-{sim.next_id('trip'):08d}"
    ctx.tms.write_trip(
        TripRow(
            tid,
            vid,
            did,
            a,
            b,
            sim.clock.datetime(slot_ms),
            sim.clock.datetime(),
            sim.clock.datetime(slot_ms + planned),
            None,
            "linehaul",
        )
    )
    ctx.tms.write_trip_stops([TripStopRow(tid, it[0], i) for i, it in enumerate(loaded)])
    for it in loaded:
        transition(sim, it[0], "LINEHAUL_DEPARTED", it[2], depot=a, driver=did)
    sim.state["vehicles"][vid].update({"at": None, "busy": True})
    sim.state["drivers"][did].update({"at": None, "busy": True})
    sim.state["trips"][tid] = {
        "kind": "linehaul",
        "vehicle": vid,
        "driver": did,
        "from": a,
        "to": b,
        "planned_dep": slot_ms,
        "dep": now,
        "planned_arr": slot_ms + planned,
        "items": loaded,
    }
    (la, oa), (lb, ob) = ctx.routes.coords[a], ctx.routes.coords[b]
    start_gps(sim, tid, vid, [[now, la, oa], [now + actual, lb, ob]])
    sim.schedule_in(actual, "linehaul.arrive", {"trip_id": tid})


def on_linehaul_arrive(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    ctx, ops, now = sim.ctx, sim.ctx.cfg.operations, sim.clock.now_ms
    tid = event.payload["trip_id"]
    trip = sim.state["trips"].pop(tid)
    b = trip["to"]
    ctx.tms.write_trip(
        TripRow(
            tid,
            trip["vehicle"],
            trip["driver"],
            trip["from"],
            b,
            sim.clock.datetime(trip["planned_dep"]),
            sim.clock.datetime(trip["dep"]),
            sim.clock.datetime(trip["planned_arr"]),
            sim.clock.datetime(),
            "linehaul",
        )
    )
    rest = ops.linehaul.driver_rest_hours * HOUR_MS
    sim.state["drivers"][trip["driver"]].update({"at": b, "busy": False, "free_ms": now + rest})
    sim.state["vehicles"][trip["vehicle"]]["at"] = b  # still busy until unloaded
    kept = []
    for it in trip["items"]:
        if _rng_hit(sim, "ops.lost", ops.exceptions.lost_per_linehaul_leg):
            transition(sim, it[0], "EXCEPTION", it[2], depot=b, reason="lost")
        else:
            kept.append(it)
    job = {"kind": "trip", "trip_id": tid, "vehicle_id": trip["vehicle"], "items": kept}
    _dock_arrive(sim, b, job)


# --- last mile ---------------------------------------------------------------------------------
def on_rider_shift(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    rider = event.payload["rider"]
    _daily(sim, "rider.shift", {"rider": rider})
    limit = sim.ctx.drivers[rider].shift_hours_limit * HOUR_MS
    sim.state["drivers"][rider]["shift_end_ms"] = sim.clock.now_ms + limit
    _try_loop(sim, rider)


def on_rider_try(engine: Engine, event: Event) -> None:
    _try_loop(as_sim(engine), event.payload["rider"])


def _try_loop(sim: SimEngine, rider: str) -> None:
    ctx, lm, now = sim.ctx, sim.ctx.cfg.operations.lastmile, sim.clock.now_ms
    st = sim.state["drivers"][rider]
    remaining = st["shift_end_ms"] - now
    if st["busy"] or remaining < lm.min_loop_minutes * MINUTE_MS:
        return
    dep = ctx.drivers[rider].home_depot
    queue: list[Item] = _depot(sim, dep)["lastmile"]
    ready = sorted(
        (it for it in queue if it[1] < now),  # strict: never two writes in one millisecond
        key=lambda it: (ctx.facts[it[0]].promised_ms, it[0]),
    )
    bike = ctx.rider_bike[rider]
    cap_g, load_g = ctx.vehicles[bike].capacity_kg * 1000, 0
    chosen: list[Item] = []
    for it in ready:
        if len(chosen) >= lm.max_stops:
            break
        w = ctx.facts[it[0]].weight_g
        if load_g + w <= cap_g:
            load_g += w
            chosen.append(it)
    while chosen:
        route, legs, total = _plan_route(sim, dep, chosen)
        if total <= remaining:
            break
        chosen = chosen[: max(0, min(len(chosen) - 1, int(len(chosen) * remaining / total)))]
    if not chosen:
        poll = lm.poll_minutes * MINUTE_MS
        if remaining - poll >= lm.min_loop_minutes * MINUTE_MS:
            sim.schedule_in(poll, "rider.try", {"rider": rider})
        return

    taken = {it[0] for it in route}
    _depot(sim, dep)["lastmile"] = [it for it in queue if it[0] not in taken]
    rng = sim.rng("lastmile.noise")
    t, stops = now, []
    for it, leg in zip(route, legs[:-1], strict=True):
        t += max(1, int(leg * math.exp(lm.stop_noise_sigma * float(rng.standard_normal()))))
        stops.append([it[0], it[2], t])
    back = max(1, int(legs[-1] * math.exp(lm.stop_noise_sigma * float(rng.standard_normal()))))
    tid = f"T-{sim.next_id('trip'):08d}"
    ctx.tms.write_trip(
        TripRow(
            tid,
            bike,
            rider,
            dep,
            dep,
            sim.clock.datetime(),
            sim.clock.datetime(),
            sim.clock.datetime(now + total),
            None,
            "lastmile",
        )
    )
    ctx.tms.write_trip_stops([TripStopRow(tid, s[0], i + 1) for i, s in enumerate(stops)])
    handheld = sim.device("rider_handheld", rider)
    for sid, attempts, _eta in stops:
        transition(sim, sid, "OUT_FOR_DELIVERY", attempts, depot=dep, device=handheld, driver=rider)
    st["busy"] = True
    sim.state["vehicles"][bike]["busy"] = True
    sim.state["trips"][tid] = {
        "kind": "lastmile",
        "vehicle": bike,
        "driver": rider,
        "depot": dep,
        "dep": now,
        "planned_arr": now + total,
        "stops": stops,
        "next": 0,
        "failed": [],
    }
    for i, (_sid, _att, eta) in enumerate(stops):
        sim.schedule(eta, "lastmile.stop", {"trip_id": tid, "idx": i})
    sim.schedule(t + back, "lastmile.return", {"trip_id": tid})
    start_gps(sim, tid, bike, _loop_waypoints(sim, dep, now, stops, t + back))


def _plan_route(sim: SimEngine, dep: str, items: list[Item]) -> tuple[list[Item], list[int], int]:
    """Nearest-neighbour tour from the depot. Returns (stop order, per-stop leg+service ms with
    the final return leg last, total ms)."""
    ctx, lm = sim.ctx, sim.ctx.cfg.operations.lastmile
    lat, lon = ctx.routes.coords[dep]
    left, route, legs = list(items), [], []
    speed = lm.speed_kmh / HOUR_MS
    while left:
        best_i, best_d = 0, math.inf
        for i, it in enumerate(left):
            f = ctx.facts[it[0]]
            d = ctx.routes.point_distance_km(lat, lon, f.lat, f.lon)
            if d < best_d:
                best_i, best_d = i, d
        it = left.pop(best_i)
        f = ctx.facts[it[0]]
        service = (lm.service_minutes + (lm.cod_extra_minutes if f.cod_vnd else 0)) * MINUTE_MS
        legs.append(int(best_d / speed) + service)
        route.append(it)
        lat, lon = f.lat, f.lon
    home_lat, home_lon = ctx.routes.coords[dep]
    legs.append(max(1, int(ctx.routes.point_distance_km(lat, lon, home_lat, home_lon) / speed)))
    return route, legs, sum(legs)


def on_lastmile_stop(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    ctx, ops = sim.ctx, sim.ctx.cfg.operations
    trip = sim.state["trips"][event.payload["trip_id"]]
    idx = event.payload["idx"]
    if idx != trip["next"]:
        raise RuntimeError(f"stop {idx} out of order on {event.payload['trip_id']}")
    trip["next"] = idx + 1
    sid, attempts, _eta = trip["stops"][idx]
    f = ctx.facts[sid]
    rider, dep = trip["driver"], trip["depot"]
    who = {"depot": dep, "device": sim.device("rider_handheld", rider), "driver": rider}
    if f.quality == "bad" and _rng_hit(
        sim, "lastmile.unresolvable", ops.exceptions.unresolvable_if_bad_address
    ):
        transition(sim, sid, "EXCEPTION", attempts + 1, reason="address_unresolvable", **who)
        return
    fp = ops.lastmile.fail_prob
    p_addr = fp.ambiguous if f.quality == "ambiguous" else fp.bad if f.quality == "bad" else 0.0
    p_cod = fp.cod if f.cod_vnd else 0.0
    r = float(sim.rng("lastmile.outcome").random())
    if r < fp.base + p_addr + p_cod:
        # The same draw says which factor failed it (no extra randomness consumed).
        reason = (
            "recipient_unavailable"
            if r < fp.base
            else "address_issue"
            if r < fp.base + p_addr
            else "cod_refused"
        )
        transition(sim, sid, "DELIVERY_FAILED", attempts + 1, reason=reason, **who)
        trip["failed"].append([sid, sim.clock.now_ms, attempts + 1])
        return
    if f.cod_vnd:
        cid = f"CC-{sim.next_id('cod'):08d}"
        ctx.tms.write_cod_collection(
            CodCollectionRow(cid, sid, trip["driver"], f.cod_vnd, sim.clock.datetime())
        )
    transition(sim, sid, "DELIVERED", attempts + 1, delivered=True, **who)


def on_lastmile_return(engine: Engine, event: Event) -> None:
    sim = as_sim(engine)
    ctx, lm = sim.ctx, sim.ctx.cfg.operations.lastmile
    tid = event.payload["trip_id"]
    trip = sim.state["trips"].pop(tid)
    dep, rider = trip["depot"], trip["driver"]
    ctx.tms.write_trip(
        TripRow(
            tid,
            trip["vehicle"],
            rider,
            dep,
            dep,
            sim.clock.datetime(trip["dep"]),
            sim.clock.datetime(trip["dep"]),
            sim.clock.datetime(trip["planned_arr"]),
            sim.clock.datetime(),
            "lastmile",
        )
    )
    for sid, failed_ms, attempts in trip["failed"]:
        if attempts >= lm.max_attempts:
            transition(sim, sid, "RETURNED", attempts, depot=dep, reason="max_attempts")
        else:
            ready = failed_ms + lm.reattempt_delay_hours * HOUR_MS
            _depot(sim, dep)["lastmile"].append([sid, ready, attempts])
    sim.state["drivers"][rider]["busy"] = False
    sim.state["vehicles"][trip["vehicle"]]["busy"] = False
    _try_loop(sim, rider)


# --- GPS ---------------------------------------------------------------------------------------
def _loop_waypoints(
    sim: SimEngine, dep: str, start: int, stops: list[list[Any]], end: int
) -> list[list[float]]:
    """Depot -> (arrive, dwell for the service time) at each stop -> depot."""
    lm = sim.ctx.cfg.operations.lastmile
    lat0, lon0 = sim.ctx.routes.coords[dep]
    wps: list[list[float]] = [[start, lat0, lon0]]
    prev = start
    for sid, _att, eta in stops:
        f = sim.ctx.facts[sid]
        service = (lm.service_minutes + (lm.cod_extra_minutes if f.cod_vnd else 0)) * MINUTE_MS
        arrive = eta - min(service, (eta - prev) // 2)
        wps += [[arrive, f.lat, f.lon], [eta, f.lat, f.lon]]
        prev = eta
    wps.append([end, lat0, lon0])
    return wps


def start_gps(sim: SimEngine, tid: str, vid: str, waypoints: list[list[float]]) -> None:
    """Allocate the trip's device_seq range and queue its whole ping batch (ADR 0010)."""
    interval = sim.ctx.cfg.emitters.gps.interval_seconds * SECOND_MS
    n = ping_count(int(waypoints[0][0]), int(waypoints[-1][0]), interval)
    base = sim.counters.take(f"{sim.ctx.cfg.emitters.topics.gps}:{vid}", n)
    sim.state["trips"][tid]["gps"] = {"base_seq": base, "waypoints": waypoints}
    sim.ctx.outbox.add_gps(gps_batch(sim, tid, sim.state["trips"][tid]))


def gps_batch(sim: SimEngine, tid: str, trip: dict[str, Any]) -> PingBatch:
    v = sim.ctx.vehicles[trip["vehicle"]]
    return generate_pings(
        vehicle_id=v.vehicle_id,
        vehicle_type=v.type,
        waypoints=trip["gps"]["waypoints"],
        base_seq=trip["gps"]["base_seq"],
        spec=sim.ctx.cfg.emitters.gps,
        circuity=sim.ctx.routes.circuity,
        rng=stream(sim.seed, f"gps.{tid}"),
    )


def registry() -> HandlerRegistry:
    reg = HandlerRegistry()
    for name, fn in (
        ("demand.tick", on_demand_tick),
        ("pickup.run", on_pickup_run),
        ("dock.arrive", on_dock_arrive),
        ("dock.done", on_dock_done),
        ("sort.local", on_sort_local),
        ("linehaul.slot", on_linehaul_slot),
        ("linehaul.retry", on_linehaul_retry),
        ("linehaul.arrive", on_linehaul_arrive),
        ("rider.shift", on_rider_shift),
        ("rider.try", on_rider_try),
        ("lastmile.stop", on_lastmile_stop),
        ("lastmile.return", on_lastmile_return),
    ):
        reg.register(name, fn)
    return reg
