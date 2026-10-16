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
        ("run", _cmd_run, "run the simulator (not yet implemented)"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--preset", default=None, help="scale preset (overrides base.yaml)")
        p.set_defaults(func=fn)
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
