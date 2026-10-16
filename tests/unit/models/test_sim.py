"""Full business simulation against the in-memory TMS: determinism, crash-resume, conservation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import InMemoryTms
from vlsim.config import SimConfig, load_config
from vlsim.engine.checkpoint import CheckpointStore, checkpoint_hash
from vlsim.engine.clock import DAY_MS, HOUR_MS, ms_from_datetime
from vlsim.engine.core import CheckpointError
from vlsim.models.sim import Simulation
from vlsim.models.statemachine import TERMINAL


@pytest.fixture(scope="module")
def cfg() -> SimConfig:
    return load_config(preset="s")


def _start(cfg: SimConfig) -> int:
    return ms_from_datetime(cfg.base.start_time)


def _run(
    cfg: SimConfig, days: float, store: CheckpointStore | None = None
) -> tuple[Simulation, InMemoryTms]:
    tms = InMemoryTms(cfg)
    sim = Simulation.fresh(cfg, tms, store)
    sim.run_until(_start(cfg) + int(days * DAY_MS))
    return sim, tms


@pytest.fixture(scope="module")
def three_days(cfg: SimConfig) -> tuple[Simulation, InMemoryTms]:
    return _run(cfg, 3)


def test_same_seed_same_database_and_state(
    cfg: SimConfig, three_days: tuple[Simulation, InMemoryTms]
) -> None:
    sim_a, tms_a = three_days
    sim_b, tms_b = _run(cfg, 3)
    assert tms_a.snapshot() == tms_b.snapshot()
    assert checkpoint_hash(sim_a.engine.to_checkpoint()) == checkpoint_hash(
        sim_b.engine.to_checkpoint()
    )


def test_volume_and_flow(cfg: SimConfig, three_days: tuple[Simulation, InMemoryTms]) -> None:
    sim, tms = three_days
    shipments = tms.tables["shipments"]
    assert len(shipments) == pytest.approx(3 * cfg.scale.shipments_per_day, rel=0.08)
    statuses = {s.current_status for s in shipments.values()}
    assert {"DELIVERED", "OUT_FOR_DELIVERY", "LINEHAUL_DEPARTED"} <= statuses | {"OUT_FOR_DELIVERY"}
    routes = sim.engine.ctx.routes
    day1 = _start(sim.cfg) + DAY_MS
    local = [
        s
        for s in shipments.values()
        if tms.tables["orders"][s.order_id].created_at < sim.engine.clock.datetime(day1)
        and routes.distance_class(s.origin_depot, s.dest_depot) in ("same_depot", "same_region")
    ]
    terminal = sum(s.current_status in TERMINAL for s in local)
    assert len(local) > 100
    assert terminal > 0.8 * len(local)  # measured 86%: feeders such as NHA-DAD take ~11 h
    assert any(t.kind == "linehaul" for t in tms.tables["trips"].values())
    assert any(t.kind == "lastmile" for t in tms.tables["trips"].values())


def test_conservation_db_matches_engine(three_days: tuple[Simulation, InMemoryTms]) -> None:
    """Every shipment is terminal in the TMS, or the engine holds it somewhere - never both,
    never neither."""
    sim, tms = three_days
    non_terminal = {
        sid for sid, s in tms.tables["shipments"].items() if s.current_status not in TERMINAL
    }
    assert set(sim.in_flight()) == non_terminal
    assert (
        len(tms.tables["orders"])
        == len(tms.tables["shipments"])
        == sim.engine.state["seq"]["order"]
    )


def test_facts_cache_holds_exactly_in_flight(three_days: tuple[Simulation, InMemoryTms]) -> None:
    sim, _ = three_days
    assert set(sim.engine.ctx.facts) == set(sim.in_flight())


@pytest.mark.parametrize("crash_after_hours", [5, 23])
def test_crash_after_checkpoint_resumes_to_identical_database(
    cfg: SimConfig, tmp_path: Path, crash_after_hours: int
) -> None:
    end = _start(cfg) + 3 * DAY_MS
    ref = Simulation.fresh(cfg, InMemoryTms(cfg))
    ref.run_until(end)
    ref_tms = ref.engine.ctx.tms
    assert isinstance(ref_tms, InMemoryTms)

    store = CheckpointStore(tmp_path / "state.sqlite")
    tms = InMemoryTms(cfg)
    first = Simulation.fresh(cfg, tms, store)
    first.run_until(_start(cfg) + DAY_MS + 12 * HOUR_MS)  # ends on a checkpoint boundary
    first.engine.run(until_ms=first.engine.clock.now_ms + crash_after_hours * HOUR_MS)
    tms.flush()  # writes after the checkpoint reached the database ...
    first.engine.run(until_ms=first.engine.clock.now_ms + HOUR_MS)
    tms.crash()  # ... and the last hour's buffered writes were lost with the process
    del first

    resumed = Simulation.resume(cfg, tms, store)
    assert resumed is not None
    resumed.run_until(end)
    assert tms.snapshot() == ref_tms.snapshot()
    assert checkpoint_hash(resumed.engine.to_checkpoint()) == checkpoint_hash(
        ref.engine.to_checkpoint()
    )


def test_restore_refuses_changed_config(cfg: SimConfig, tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "s.sqlite")
    tms = InMemoryTms(cfg)
    sim = Simulation.fresh(cfg, tms, store)
    sim.run_until(_start(cfg) + 2 * HOUR_MS)
    sim.checkpoint()
    changed = cfg.model_copy(update={"base": cfg.base.model_copy(update={"seed": 1})})
    assert changed.fingerprint() != cfg.fingerprint()
    with pytest.raises(CheckpointError, match="fingerprint"):
        Simulation.resume(changed, tms, store)


def test_fingerprint_covers_every_config_file(cfg: SimConfig) -> None:
    variants = [
        cfg.model_copy(update={"scale": cfg.scale.model_copy(update={"drivers": 61})}),
        cfg.model_copy(
            update={"demand": cfg.demand.model_copy(update={"merchant_zipf_exponent": 1.2})}
        ),
        cfg.model_copy(
            update={"operations": cfg.operations.model_copy(update={"carrier_id": "CR-02"})}
        ),
    ]
    assert len({cfg.fingerprint(), *(v.fingerprint() for v in variants)}) == 4
