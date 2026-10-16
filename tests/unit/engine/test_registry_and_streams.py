from __future__ import annotations

import numpy as np
import pytest

from vlsim.engine.core import Engine, HandlerRegistry
from vlsim.engine.events import Event
from vlsim.engine.rng import RngStreams


def _noop(engine: Engine, event: Event) -> None:
    pass


def test_registry_rejects_duplicates_and_unknown() -> None:
    reg = HandlerRegistry()
    reg.register("a", _noop)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("a", _noop)
    with pytest.raises(KeyError):
        reg.get("missing")
    assert reg.names() == ("a",)


def test_registry_rejects_lambdas_and_closures() -> None:
    reg = HandlerRegistry()
    with pytest.raises(TypeError):
        reg.register("l", lambda e, ev: None)

    captured = 1

    def closure(engine: Engine, event: Event) -> None:
        _ = captured

    with pytest.raises(TypeError):
        reg.register("c", closure)


def test_schedule_unknown_handler_fails_fast() -> None:
    eng = Engine(seed=1, start_ms=0, offset_minutes=420, registry=HandlerRegistry())
    with pytest.raises(KeyError):
        eng.schedule(10, "nope", {})


def test_schedule_in_past_rejected() -> None:
    reg = HandlerRegistry()
    reg.register("n", _noop)
    eng = Engine(seed=1, start_ms=100, offset_minutes=420, registry=reg)
    with pytest.raises(ValueError, match="past"):
        eng.schedule(99, "n", {})
    eng.schedule(100, "n", {})  # same instant is allowed


def test_rng_streams_state_round_trip() -> None:
    s = RngStreams(seed=42)
    s.get("b").random(3)
    s.get("a").integers(0, 10, 5)
    state = s.state()
    assert list(state) == ["a", "b"]  # sorted by name
    expected = {n: s.get(n).random(4) for n in ("a", "b")}
    r = RngStreams.restore(42, state)
    for n in ("a", "b"):
        assert np.array_equal(r.get(n).random(4), expected[n])


def test_rng_streams_match_plain_stream_function() -> None:
    from vlsim.engine.rng import stream  # noqa: PLC0415

    assert np.array_equal(RngStreams(7).get("x").random(3), stream(7, "x").random(3))
