"""TMS connection settings, read from the environment."""

from __future__ import annotations

import os

DEFAULT_DSN = "postgresql+psycopg://vlsim:vlsim@localhost:55432/tms"


def tms_dsn() -> str:
    return os.environ.get("VLSIM_TMS_DSN", DEFAULT_DSN)
