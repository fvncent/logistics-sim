"""M4 gates against real Postgres: a week at S scale (conservation, no double-booking) and a hard
crash after the last checkpoint that resumes to an identical database."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from tests.integration.conftest import ScratchDatabases, dump, seeded
from vlsim.config import SimConfig, load_config
from vlsim.engine.checkpoint import CheckpointStore, checkpoint_hash
from vlsim.engine.clock import DAY_MS, HOUR_MS, ms_from_datetime
from vlsim.models.sim import Simulation
from vlsim.models.statemachine import TERMINAL
from vlsim.tms.service import PostgresTms

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cfg() -> SimConfig:
    return load_config(preset="s")


@pytest.fixture(scope="module")
def week(cfg: SimConfig) -> Any:
    dbs = ScratchDatabases()
    engine = seeded(dbs.create("vlsim_week"), cfg)
    sim = Simulation.fresh(cfg, PostgresTms(engine))
    sim.run_until(ms_from_datetime(cfg.base.start_time) + 7 * DAY_MS)
    yield sim, engine
    dbs.close()


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql)))


def test_week_every_shipment_terminal_or_held_by_engine(week: Any) -> None:
    sim, engine = week
    rows = _rows(engine, "SELECT shipment_id, current_status FROM shipments")
    non_terminal = {sid for sid, st in rows if st not in TERMINAL}
    held = sim.in_flight()
    assert set(held) == non_terminal  # nothing lost, nothing double-held, nothing phantom
    n_orders = _rows(engine, "SELECT count(*) FROM orders")[0][0]
    assert n_orders == len(rows) == sim.engine.state["seq"]["order"]


def test_week_early_shipments_all_finished(week: Any, cfg: SimConfig) -> None:
    day1 = cfg.base.start_time + timedelta(days=1)
    open_early = _rows(
        engine := week[1],
        "SELECT s.shipment_id, s.current_status FROM shipments s JOIN orders o USING (order_id) "
        f"WHERE o.created_at < '{day1.isoformat()}' AND s.current_status NOT IN "
        "('DELIVERED','RETURNED','EXCEPTION')",
    )
    assert open_early == [], f"{len(open_early)} day-1 shipments still open after a week"
    assert engine is not None


def test_week_volume_and_outcomes(week: Any, cfg: SimConfig) -> None:
    _sim, engine = week
    per_day = _rows(engine, "SELECT count(*) FROM orders")[0][0] / 7
    assert per_day == pytest.approx(cfg.scale.shipments_per_day, rel=0.06)
    by = dict(_rows(engine, "SELECT current_status, count(*) FROM shipments GROUP BY 1"))
    finished = by.get("DELIVERED", 0) + by.get("RETURNED", 0) + by.get("EXCEPTION", 0)
    assert by["DELIVERED"] / finished > 0.9
    assert _rows(engine, "SELECT count(*) FROM shipments WHERE current_status = 'RETURNED' "
                         "AND attempt_count <> 3")[0][0] == 0  # fmt: skip
    assert _rows(engine, "SELECT count(*) FROM shipments WHERE current_status = 'DELIVERED' "
                         "AND attempt_count NOT BETWEEN 1 AND 3")[0][0] == 0  # fmt: skip


def test_week_cod_collected_exactly_once_per_cod_delivery(week: Any) -> None:
    _sim, engine = week
    bad = _rows(
        engine,
        "SELECT s.shipment_id FROM shipments s JOIN orders o USING (order_id) "
        "LEFT JOIN cod_collections c USING (shipment_id) "
        "WHERE (s.current_status = 'DELIVERED' AND o.cod_amount_vnd > 0) "
        "  <> (c.collection_id IS NOT NULL) "
        "OR (c.amount_vnd IS NOT NULL AND c.amount_vnd <> o.cod_amount_vnd)",
    )
    assert bad == []
    dup = _rows(engine, "SELECT shipment_id FROM cod_collections GROUP BY 1 HAVING count(*) > 1")
    assert dup == []


def _intervals_disjoint(rows: list[Any], key_idx: int) -> list[str]:
    """rows: (key, trip_id, dep, arr|None, from, to). Per key: no overlap, at most one open trip
    (the last), and each trip starts where the previous one ended (no teleporting)."""
    by: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by[r[key_idx]].append(r)
    problems = []
    for key, trips in sorted(by.items()):
        trips.sort(key=lambda r: (r[2], r[1]))
        for prev, nxt in pairwise(trips):
            if prev[3] is None or nxt[2] < prev[3]:
                problems.append(f"{key}: {prev[1]} overlaps {nxt[1]}")
            if prev[5] != nxt[4]:
                problems.append(f"{key}: {prev[1]} ends at {prev[5]}, {nxt[1]} starts at {nxt[4]}")
    return problems


def test_week_no_vehicle_or_driver_double_booked(week: Any) -> None:
    _sim, engine = week
    rows = _rows(
        engine,
        "SELECT vehicle_id, trip_id, actual_dep, actual_arr, from_depot, to_depot, driver_id "
        "FROM trips",
    )
    assert _intervals_disjoint(rows, 0) == []
    drivers = [(r[6], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    assert _intervals_disjoint(drivers, 0) == []


def test_week_no_parcel_double_booked(week: Any) -> None:
    _sim, engine = week
    rows = _rows(
        engine,
        "SELECT ts.shipment_id, t.trip_id, t.actual_dep, t.actual_arr, t.from_depot, t.to_depot, "
        "t.kind FROM trip_shipments ts JOIN trips t USING (trip_id)",
    )
    by: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    problems = []
    for sid, trips in by.items():
        trips.sort(key=lambda r: (r[2], r[1]))
        for prev, nxt in pairwise(trips):
            if prev[3] is None or nxt[2] < prev[3]:
                problems.append(f"{sid}: on {prev[1]} and {nxt[1]} at once")
    assert problems == []
    status = dict(_rows(engine, "SELECT shipment_id, current_status FROM shipments"))
    for sid, trips in by.items():
        last = max(trips, key=lambda r: (r[2], r[1]))
        if last[3] is None and last[6] == "linehaul":
            assert status[sid] in ("LINEHAUL_DEPARTED", "EXCEPTION"), sid


def test_week_riders_plan_within_shift(week: Any, cfg: SimConfig) -> None:
    sim, engine = week
    offset = sim.engine.clock.offset_minutes
    rows = _rows(
        engine,
        "SELECT t.trip_id, t.driver_id, t.actual_dep, t.planned_arr, d.shift_start, "
        "d.shift_hours_limit FROM trips t JOIN drivers d USING (driver_id) "
        "WHERE t.kind = 'lastmile'",
    )
    assert rows
    for tid, did, dep, planned_arr, shift_start, limit in rows:
        local = sim.engine.clock.datetime(ms_from_datetime(dep))
        start = datetime.combine(local.date(), shift_start, tzinfo=local.tzinfo)
        if start > local:
            start -= timedelta(days=1)
        end = start + timedelta(hours=limit)
        assert local >= start and planned_arr <= end, (tid, did, local, planned_arr, end, offset)


def test_crash_after_checkpoint_resumes_to_identical_database(
    cfg: SimConfig, scratch_dbs: ScratchDatabases, tmp_path: Path
) -> None:
    start = ms_from_datetime(cfg.base.start_time)
    end = start + 3 * DAY_MS

    ref_db = seeded(scratch_dbs.create("vlsim_ref"), cfg)
    ref = Simulation.fresh(cfg, PostgresTms(ref_db))
    ref.run_until(end)

    crash_db = seeded(scratch_dbs.create("vlsim_crash"), cfg)
    store = CheckpointStore(tmp_path / "state.sqlite")
    tms = PostgresTms(crash_db)
    first = Simulation.fresh(cfg, tms, store)
    first.run_until(start + DAY_MS + 12 * HOUR_MS)  # last checkpoint at 36 h
    first.engine.run(until_ms=first.engine.clock.now_ms + 9 * HOUR_MS)
    tms.flush()  # 9 h of post-checkpoint writes reach the database ...
    first.engine.run(until_ms=first.engine.clock.now_ms + 2 * HOUR_MS)
    assert tms.pending() > 0
    tms.discard_buffer()  # ... then the process dies with 2 h of writes unflushed
    del first

    resumed = Simulation.resume(cfg, PostgresTms(crash_db), store)
    assert resumed is not None
    resumed.run_until(end)

    assert dump(crash_db) == dump(ref_db)
    assert checkpoint_hash(resumed.engine.to_checkpoint()) == checkpoint_hash(
        ref.engine.to_checkpoint()
    )
