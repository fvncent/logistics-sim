from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from vlsim.engine.clock import SimClock, ms_from_datetime, ms_to_datetime

ICT = timezone(timedelta(hours=7))


def test_ms_round_trip_is_exact() -> None:
    dt = datetime(2026, 10, 16, 9, 41, 12, 345_000, tzinfo=ICT)
    ms = ms_from_datetime(dt)
    assert isinstance(ms, int)
    assert ms_to_datetime(ms, offset_minutes=420) == dt
    assert ms_from_datetime(dt.astimezone(UTC)) == ms  # absolute instant, offset-independent


def test_sub_millisecond_rejected() -> None:
    with pytest.raises(ValueError, match="millisecond"):
        ms_from_datetime(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC))


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="offset"):
        ms_from_datetime(datetime(2026, 1, 1))  # noqa: DTZ001


def test_isoformat_has_explicit_offset_and_millis() -> None:
    clock = SimClock(ms_from_datetime(datetime(2026, 10, 16, 9, 41, 12, tzinfo=ICT)), 420)
    assert clock.isoformat() == "2026-10-16T09:41:12.000+07:00"
    assert clock.isoformat(clock.now_ms + 1) == "2026-10-16T09:41:12.001+07:00"


def test_clock_only_moves_forward() -> None:
    clock = SimClock(1_000, 420)
    clock.advance_to(1_000)
    clock.advance_to(2_500)
    assert clock.now_ms == 2_500
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(2_499)


@pytest.mark.parametrize("bad", [1.5, True, "10"])
def test_clock_requires_int_ms(bad: object) -> None:
    with pytest.raises(TypeError):
        SimClock(0, 420).advance_to(bad)  # type: ignore[arg-type]
