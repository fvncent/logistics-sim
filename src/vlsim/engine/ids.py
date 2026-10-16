"""Per-logical-event emission counters and deterministic event IDs.

Each emitted logical event takes the next counter value for its kind (e.g. a topic). The
event_id is UUIDv5 over (kind, counter, canonical content), so the same run gives the same IDs,
and a fault-injected duplicate reuses the original event's ID. Counters are checkpointed.
"""

from __future__ import annotations

import uuid
from typing import Any

from vlsim.engine.canonical import canonical_json

# Fixed namespace for all simulator event IDs. Changing it changes every event_id.
EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "sim.vietlink-express.invalid")


class EmissionCounters:
    __slots__ = ("_counts",)

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def next(self, kind: str) -> int:
        n = self._counts.get(kind, 0) + 1
        self._counts[kind] = n
        return n

    def data(self) -> dict[str, int]:
        return {k: self._counts[k] for k in sorted(self._counts)}

    @classmethod
    def restore(cls, data: dict[str, int]) -> EmissionCounters:
        c = cls()
        c._counts = {str(k): int(v) for k, v in sorted(data.items())}
        return c

    @staticmethod
    def event_id(kind: str, n: int, content: dict[str, Any]) -> uuid.UUID:
        return uuid.uuid5(EVENT_NAMESPACE, canonical_json([kind, n, content]))
