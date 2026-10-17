"""Fault injector interface. MVP: pass-through only (SPEC: "the MVP has none").

Sits between ground truth and the producer: ground truth records the true event, the injector
may later duplicate, delay, drop or corrupt what the platform receives (v2/v3), logging each
fault with an id.
"""

from __future__ import annotations

from typing import Any, Protocol


class FaultInjector(Protocol):
    def process(
        self, topic: str, key: str, event: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]: ...


class PassThrough:
    def process(
        self, topic: str, key: str, event: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        return [(key, event)]
