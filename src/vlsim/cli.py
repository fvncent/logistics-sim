"""Command-line entry point: `vlsim <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import structlog

from vlsim.config import DEFAULT_CONFIG_DIR, load_config
from vlsim.log_setup import configure_logging

log = structlog.get_logger()


def _config_dir() -> Path:
    return Path(os.environ.get("VLSIM_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))


def _cmd_config_check(args: argparse.Namespace) -> int:
    cfg = load_config(_config_dir(), args.preset)
    log.info(
        "config_ok",
        config_version=cfg.base.config_version,
        preset=cfg.scale.name,
        vehicles=cfg.scale.vehicle_count,
        shipments_per_day=cfg.scale.shipments_per_day,
        depots=len(cfg.network.depots),
        lanes=len(cfg.network.lanes),
    )
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    # Imported here so the engine-side commands never load the TMS database layer.
    from sqlalchemy import create_engine  # noqa: PLC0415

    from vlsim.tms.seed import seed_world  # noqa: PLC0415
    from vlsim.tms.settings import tms_dsn  # noqa: PLC0415
    from vlsim.world.build import build_world  # noqa: PLC0415

    cfg = load_config(_config_dir(), args.preset)
    world = build_world(cfg)
    engine = create_engine(tms_dsn())
    with engine.begin() as conn:
        written = seed_world(conn, world)
    engine.dispose()
    log.info("seed_done", preset=cfg.scale.name, seed=cfg.base.seed, rows_written=written)
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    """Batch run (no wall-clock pacing): resume from the checkpoint store or start fresh."""
    import json  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415

    from vlsim.engine.checkpoint import CheckpointStore  # noqa: PLC0415
    from vlsim.engine.clock import DAY_MS  # noqa: PLC0415
    from vlsim.models.sim import Simulation  # noqa: PLC0415
    from vlsim.tms.service import PostgresTms  # noqa: PLC0415
    from vlsim.tms.settings import tms_dsn  # noqa: PLC0415

    cfg = load_config(_config_dir(), args.preset)
    tms = PostgresTms(create_engine(tms_dsn()))
    store = CheckpointStore(Path(args.state_dir) / "checkpoints.sqlite")
    sim = Simulation.open(cfg, tms, store)
    start_events = sim.engine.events_processed
    end = sim.engine.clock.now_ms + int(args.days * DAY_MS)
    sim.run_until(end)
    sim.checkpoint()
    summary = sim.summary() | {
        "events_this_run": sim.engine.events_processed - start_events,
        "tms_rows_flushed": tms.rows_flushed,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    load_config(_config_dir(), args.preset)
    log.error("engine_not_implemented", detail="the simulation loop lands in M3-M6")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlsim")
    parser.add_argument("--log-level", default=os.environ.get("VLSIM_LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, help_ in (
        ("config-check", _cmd_config_check, "validate config and print a summary"),
        ("seed", _cmd_seed, "upsert deterministic master data into TMS Postgres"),
        ("simulate", _cmd_simulate, "batch-run N sim days against TMS Postgres (resumes)"),
        ("run", _cmd_run, "run the simulator live (not yet implemented)"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--preset", default=None, help="scale preset (overrides base.yaml)")
        if name == "simulate":
            p.add_argument("--days", type=float, default=7.0)
            p.add_argument("--state-dir", default=os.environ.get("VLSIM_STATE_DIR", "data/state"))
        p.set_defaults(func=fn)
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
