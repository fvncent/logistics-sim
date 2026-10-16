"""Shipment state machine (confirmed 2026-10-16; EXCEPTION terminal per ADR 0005).

CREATED -> PICKED_UP -> AT_ORIGIN_DEPOT -> LINEHAUL_DEPARTED -> AT_HUB (-> LINEHAUL_DEPARTED ...)
-> AT_DEST_DEPOT -> OUT_FOR_DELIVERY -> DELIVERED | DELIVERY_FAILED (-> OUT_FOR_DELIVERY, at
most 3 attempts, then RETURNED). EXCEPTION can interrupt any non-terminal stage.
"""

from __future__ import annotations

SHIPMENT_STATUSES = (
    "CREATED",
    "PICKED_UP",
    "AT_ORIGIN_DEPOT",
    "LINEHAUL_DEPARTED",
    "AT_HUB",
    "AT_DEST_DEPOT",
    "OUT_FOR_DELIVERY",
    "DELIVERY_FAILED",
    "DELIVERED",
    "RETURNED",
    "EXCEPTION",
)
TERMINAL = frozenset({"DELIVERED", "RETURNED", "EXCEPTION"})

_X = ("EXCEPTION",)
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "CREATED": ("PICKED_UP", *_X),
    "PICKED_UP": ("AT_ORIGIN_DEPOT", *_X),
    # AT_DEST_DEPOT directly when origin == dest (sorted locally, no line-haul leg).
    "AT_ORIGIN_DEPOT": ("LINEHAUL_DEPARTED", "AT_DEST_DEPOT", *_X),
    "LINEHAUL_DEPARTED": ("AT_HUB", "AT_DEST_DEPOT", *_X),
    "AT_HUB": ("LINEHAUL_DEPARTED", *_X),
    "AT_DEST_DEPOT": ("OUT_FOR_DELIVERY", *_X),
    "OUT_FOR_DELIVERY": ("DELIVERED", "DELIVERY_FAILED", *_X),
    "DELIVERY_FAILED": ("OUT_FOR_DELIVERY", "RETURNED", *_X),
    "DELIVERED": (),
    "RETURNED": (),
    "EXCEPTION": (),
}

ORDER_STATUS_FOR = {
    "CREATED": "CREATED",
    "PICKED_UP": "IN_FULFILLMENT",
    "DELIVERED": "DELIVERED",
    "RETURNED": "RETURNED",
    "EXCEPTION": "EXCEPTION",
}
"""Shipment transitions that change the order's status (others leave it unchanged)."""

EXCEPTION_REASONS = ("damage", "lost", "address_unresolvable")


class IllegalTransitionError(ValueError):
    pass


def check_transition(old: str | None, new: str) -> None:
    if old is None:
        if new != "CREATED":
            raise IllegalTransitionError(f"a new shipment must start CREATED, not {new}")
        return
    if new not in TRANSITIONS.get(old, ()):
        raise IllegalTransitionError(f"{old} -> {new} is not allowed")
