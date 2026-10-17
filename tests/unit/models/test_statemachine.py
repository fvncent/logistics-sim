from __future__ import annotations

from pathlib import Path

import pytest

from vlsim.config import load_config
from vlsim.models.statemachine import (
    SHIPMENT_STATUSES,
    TERMINAL,
    TRANSITIONS,
    IllegalTransitionError,
    check_transition,
)
from vlsim.tms import schema


def test_statuses_match_schema_and_adr_0005() -> None:
    assert SHIPMENT_STATUSES == schema.SHIPMENT_STATUSES
    assert (
        set(schema.TERMINAL_SHIPMENT_STATUSES) == TERMINAL == {"DELIVERED", "RETURNED", "EXCEPTION"}
    )
    assert load_config().operations.lastmile.max_attempts == schema.MAX_DELIVERY_ATTEMPTS


def test_terminal_states_have_no_exits() -> None:
    for s in TERMINAL:
        assert TRANSITIONS[s] == ()


def test_exception_reachable_from_every_non_terminal_state() -> None:
    for s in SHIPMENT_STATUSES:
        if s not in TERMINAL:
            assert "EXCEPTION" in TRANSITIONS[s], s


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (None, "CREATED"),
        ("CREATED", "PICKED_UP"),
        ("PICKED_UP", "AT_ORIGIN_DEPOT"),
        ("AT_ORIGIN_DEPOT", "LINEHAUL_DEPARTED"),
        ("AT_ORIGIN_DEPOT", "AT_DEST_DEPOT"),  # same-depot shipment, sorted locally
        ("LINEHAUL_DEPARTED", "AT_HUB"),
        ("AT_HUB", "LINEHAUL_DEPARTED"),  # loop for further legs
        ("LINEHAUL_DEPARTED", "AT_DEST_DEPOT"),
        ("AT_DEST_DEPOT", "OUT_FOR_DELIVERY"),
        ("OUT_FOR_DELIVERY", "DELIVERED"),
        ("OUT_FOR_DELIVERY", "DELIVERY_FAILED"),
        ("DELIVERY_FAILED", "OUT_FOR_DELIVERY"),  # reschedule
        ("DELIVERY_FAILED", "RETURNED"),  # after the last attempt
        ("AT_HUB", "EXCEPTION"),
    ],
)
def test_legal(old: str | None, new: str) -> None:
    check_transition(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (None, "PICKED_UP"),
        ("CREATED", "DELIVERED"),
        ("DELIVERED", "OUT_FOR_DELIVERY"),
        ("EXCEPTION", "AT_HUB"),
        ("RETURNED", "OUT_FOR_DELIVERY"),
        ("OUT_FOR_DELIVERY", "RETURNED"),  # must pass through DELIVERY_FAILED
        ("AT_HUB", "AT_HUB"),
    ],
)
def test_illegal(old: str | None, new: str) -> None:
    with pytest.raises(IllegalTransitionError):
        check_transition(old, new)


def test_state_machine_doc_lists_every_transition() -> None:
    doc = (Path(__file__).resolve().parents[3] / "docs" / "STATE_MACHINE.md").read_text()
    for old, news in TRANSITIONS.items():
        for new in news:
            if new == "EXCEPTION":
                continue  # documented once as "any non-terminal -> EXCEPTION"
            assert f"    {old} --> {new}" in doc, f"{old} -> {new} missing from the diagram"
    assert "any non-terminal | EXCEPTION" in doc
