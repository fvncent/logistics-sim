"""Events waiting for the next flush. Not checkpointed: a flush always empties it of everything
up to the flush instant, and on resume it is rebuilt (scans by replay, GPS by regeneration)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from vlsim.emitters.gps import PingBatch


@dataclass(frozen=True, slots=True)
class GpsPing:
    vehicle_id: str
    t_ms: int
    lat: float
    lon: float
    speed: float
    fuel: float
    seq: int


class Outbox:
    def __init__(self) -> None:
        self.scans: list[tuple[str, dict[str, Any]]] = []
        self._batches: list[list[Any]] = []  # [PingBatch, next index]

    def add_scan(self, key: str, event: dict[str, Any]) -> None:
        self.scans.append((key, event))

    def add_gps(self, batch: PingBatch, after_ms: int | None = None) -> None:
        start = 0 if after_ms is None else int(np.searchsorted(batch.t_ms, after_ms, side="right"))
        if start < batch.t_ms.size:
            self._batches.append([batch, start])

    def pending_pings(self) -> int:
        return sum(int(b.t_ms.size) - i for b, i in self._batches)

    def release(self, until_ms: int) -> tuple[list[tuple[str, dict[str, Any]]], list[GpsPing]]:
        """All scans (creation order) and every ping with t <= until_ms, ordered by
        (time, vehicle, device_seq); that order does not depend on the flush cadence."""
        scans, self.scans = self.scans, []
        pings: list[GpsPing] = []
        keep = []
        for entry in self._batches:
            b, i = entry
            j = int(np.searchsorted(b.t_ms, until_ms, side="right"))
            if j > i:
                pings.extend(
                    GpsPing(b.vehicle_id, t, la, lo, sp, fu, sq)
                    for t, la, lo, sp, fu, sq in zip(
                        b.t_ms[i:j].tolist(),
                        b.lat[i:j].tolist(),
                        b.lon[i:j].tolist(),
                        b.speed[i:j].tolist(),
                        b.fuel[i:j].tolist(),
                        b.seq[i:j].tolist(),
                        strict=True,
                    )
                )
                entry[1] = j
            if entry[1] < b.t_ms.size:
                keep.append(entry)
        self._batches = keep
        pings.sort(key=lambda p: (p.t_ms, p.vehicle_id, p.seq))
        return scans, pings
