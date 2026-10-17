"""Deployment invariants checked statically on the Compose files (no Docker needed)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = yaml.safe_load((ROOT / "compose.yaml").read_text())
SERVER = yaml.safe_load((ROOT / "compose.server.yaml").read_text())
DATA = yaml.safe_load((ROOT / "compose.server-data.yaml").read_text())
SERVICES = ("tms-postgres", "redpanda", "tms-service", "sim-engine")


def _bytes(limit: str) -> int:
    n, unit = re.fullmatch(r"(\d+)([kmg])", limit.lower()).groups()  # type: ignore[union-attr]
    return int(n) * {"k": 1024, "m": 1024**2, "g": 1024**3}[unit]


@pytest.mark.parametrize("name", SERVICES)
def test_restart_limits_and_log_rotation(name: str) -> None:
    svc = BASE["services"][name]
    assert svc["restart"] == "unless-stopped"
    assert svc["mem_limit"] == svc["memswap_limit"]  # no swap
    assert svc["logging"]["driver"] == "json-file"
    assert {"max-size", "max-file"} <= set(svc["logging"]["options"])


def test_sim_engine_memory_under_512_mb() -> None:
    assert _bytes(BASE["services"]["sim-engine"]["mem_limit"]) < 512 * 1024**2


@pytest.mark.parametrize("name", SERVICES)
def test_ports_bound_to_configurable_local_address(name: str) -> None:
    for port in BASE["services"][name].get("ports", []):
        assert port.startswith("${BIND_ADDR:-127.0.0.1}:"), port


def test_no_secrets_in_server_files() -> None:
    text = (ROOT / "compose.server.yaml").read_text() + (
        ROOT / "deploy/server.env.example"
    ).read_text()
    assert "${TMS_PG_PASSWORD:?" in text
    env = (ROOT / "deploy/server.env.example").read_text()
    assert re.search(r"^TMS_PG_PASSWORD=CHANGE_ME$", env, re.M)
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


def test_server_redpanda_is_durable_with_retention() -> None:
    args = SERVER["services"]["redpanda"]["command"]
    assert "--mode=dev-container" not in args
    sets = {args[i + 1] for i, a in enumerate(args) if a == "--set"}
    assert {"redpanda.developer_mode=false", "redpanda.write_caching_default=false",
            "redpanda.log_retention_ms=604800000"} <= sets  # fmt: skip


def test_server_data_bind_mounts_cover_every_volume() -> None:
    assert set(DATA["volumes"]) == set(BASE["volumes"])
    for v in DATA["volumes"].values():
        assert v["driver_opts"]["o"] == "bind" and "${DATA_ROOT" in v["driver_opts"]["device"]
