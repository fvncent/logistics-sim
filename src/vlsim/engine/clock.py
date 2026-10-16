"""Simulated time as integer milliseconds since the Unix epoch (UTC). Never floats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)

SECOND_MS = 1_000
MINUTE_MS = 60 * SECOND_MS
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS


def _require_ms(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{what} must be int milliseconds, got {type(value).__name__}")
    return value


def ms_from_datetime(dt: datetime) -> int:
    """Exact conversion of an offset-aware datetime to epoch milliseconds."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must carry an explicit UTC offset")
    delta = dt - EPOCH
    if delta.microseconds % 1000:
        raise ValueError("sim time has millisecond resolution; got sub-millisecond datetime")
    return delta // _MS


def ms_to_datetime(ms: int, offset_minutes: int) -> datetime:
    tz = timezone(timedelta(minutes=offset_minutes))
    return (EPOCH + timedelta(milliseconds=_require_ms(ms, "ms"))).astimezone(tz)


class SimClock:
    """Monotonic simulated clock. The engine advances it; handlers only read it."""

    __slots__ = ("_now_ms", "offset_minutes")

    def __init__(self, now_ms: int, offset_minutes: int) -> None:
        self._now_ms = _require_ms(now_ms, "now_ms")
        self.offset_minutes = _require_ms(offset_minutes, "offset_minutes")

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def advance_to(self, t_ms: int) -> None:
        _require_ms(t_ms, "t_ms")
        if t_ms < self._now_ms:
            raise ValueError(f"clock cannot move backwards ({t_ms} < {self._now_ms})")
        self._now_ms = t_ms

    def datetime(self, t_ms: int | None = None) -> datetime:
        return ms_to_datetime(self._now_ms if t_ms is None else t_ms, self.offset_minutes)

    def isoformat(self, t_ms: int | None = None) -> str:
        """ISO-8601 with millisecond precision and the explicit offset, e.g. ...12.000+07:00."""
        return self.datetime(t_ms).isoformat(timespec="milliseconds")


def local_midnight_ms(t_ms: int, offset_minutes: int) -> int:
    """Start of the local calendar day containing t_ms."""
    off = offset_minutes * MINUTE_MS
    return ((t_ms + off) // DAY_MS) * DAY_MS - off


def time_of_day_ms(hour: int, minute: int = 0) -> int:
    return hour * HOUR_MS + minute * MINUTE_MS


def next_local_time_ms(t_ms: int, hour: int, minute: int, offset_minutes: int) -> int:
    """First instant >= t_ms whose local wall time is hour:minute."""
    candidate = local_midnight_ms(t_ms, offset_minutes) + time_of_day_ms(hour, minute)
    return candidate if candidate >= t_ms else candidate + DAY_MS
