"""Canonical plain-data JSON: the only serialization used for events and checkpoints."""

from __future__ import annotations

import json
import math
from typing import Any


def plain_copy(obj: Any, _path: str = "$") -> Any:
    """Deep-copy `obj` as JSON plain data, rejecting anything that is not.

    Allowed: None, bool, int, finite float, str, list/tuple (copied as list), dict with str keys.
    Rejected: callables, sets, objects, NaN/inf, non-str keys (json would silently stringify).
    """
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"{_path}: non-finite float")
        return obj
    if isinstance(obj, list | tuple):
        return [plain_copy(v, f"{_path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"{_path}: dict key {k!r} is not a str")
            out[k] = plain_copy(v, f"{_path}.{k}")
        return out
    raise TypeError(f"{_path}: {type(obj).__name__} is not plain data")


def canonical_json(obj: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 text (no ASCII escaping), no NaN. Stable for hashing."""
    return json.dumps(
        plain_copy(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
