"""TMS connection settings, read from the environment."""

from __future__ import annotations

import os

# Host port of the Compose tms-postgres service. Not 5432: a native Postgres may hold it (ADR 0004).
DEFAULT_TMS_PG_PORT = "55432"


def tms_dsn() -> str:
    """VLSIM_TMS_DSN wins; otherwise localhost on TMS_PG_PORT (same variable compose.yaml uses)."""
    if dsn := os.environ.get("VLSIM_TMS_DSN"):
        return dsn
    port = os.environ.get("TMS_PG_PORT", DEFAULT_TMS_PG_PORT)
    return f"postgresql+psycopg://vlsim:vlsim@localhost:{port}/tms"
