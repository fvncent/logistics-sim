"""Health of the sim-engine: ok | degraded (Redpanda unreachable or spilling) | failing
(Postgres unreachable). Probes are injected callables so they can be tested and cached."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class HealthChecker:
    def __init__(
        self,
        *,
        postgres_ok: Callable[[], bool],
        redpanda_ok: Callable[[], bool],
        spill_backlog: Callable[[], int],
    ) -> None:
        self.postgres_ok = postgres_ok
        self.redpanda_ok = redpanda_ok
        self.spill_backlog = spill_backlog

    def status(self) -> dict[str, Any]:
        pg, rp, backlog = self.postgres_ok(), self.redpanda_ok(), self.spill_backlog()
        if not pg:
            status = "failing"  # business writes cannot commit; nothing can be emitted
        elif not rp or backlog > 0:
            status = "degraded"  # still simulating; events buffered on disk
        else:
            status = "ok"
        return {
            "status": status,
            "postgres": "ok" if pg else "unreachable",
            "redpanda": "ok" if rp else "unreachable",
            "spill_backlog_bytes": backlog,
        }
