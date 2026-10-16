"""Single source of truth for the TMS Postgres image and server settings: compose.yaml.

Tests read the expected version and settings from here, and CI applies the same settings to
its service container (`python -m tests.compose_info alter-system`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _service() -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    svc: dict[str, Any] = compose["services"]["tms-postgres"]
    return svc


def pg_image() -> str:
    image: str = _service()["image"]
    return image


def pg_version() -> str:
    """'postgres:17.11-bookworm' -> '17.11'."""
    m = re.match(r"^postgres:(\d+\.\d+)", pg_image())
    if not m:
        raise ValueError(f"cannot parse Postgres version from {pg_image()!r}")
    return m.group(1)


def pg_settings() -> dict[str, str]:
    """`-c key=value` pairs from the tms-postgres command."""
    cmd = [str(a) for a in _service().get("command", [])]
    return dict(cmd[i + 1].split("=", 1) for i, a in enumerate(cmd) if a == "-c")


if __name__ == "__main__":
    if sys.argv[1:] == ["alter-system"]:
        for key, value in pg_settings().items():
            print(f"ALTER SYSTEM SET {key} = '{value}';")
    elif sys.argv[1:] == ["image"]:
        print(pg_image())
    else:
        sys.exit("usage: python -m tests.compose_info alter-system|image")
