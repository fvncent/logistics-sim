from __future__ import annotations

import numpy as np

from vlsim.emitters.gps import PingBatch
from vlsim.emitters.outbox import Outbox


def _batch(vid: str, times: list[int], seq0: int) -> PingBatch:
    n = len(times)
    t = np.array(times, dtype=np.int64)
    z = np.zeros(n)
    return PingBatch(vid, "truck", t, z, z, z, z, np.arange(seq0, seq0 + n, dtype=np.int64))


def test_scans_release_in_creation_order_gps_by_time() -> None:
    ob = Outbox()
    ob.add_scan("S-2", {"x": 2})
    ob.add_scan("S-1", {"x": 1})
    ob.add_gps(_batch("V-0002", [10, 20, 30], 0))
    ob.add_gps(_batch("V-0001", [20, 25], 7))
    scans, gps = ob.release(20)
    assert [k for k, _ in scans] == ["S-2", "S-1"]
    assert [(p.vehicle_id, p.t_ms, p.seq) for p in gps] == [
        ("V-0002", 10, 0),
        ("V-0001", 20, 7),
        ("V-0002", 20, 1),
    ]
    scans, gps = ob.release(100)
    assert scans == [] and [(p.vehicle_id, p.t_ms) for p in gps] == [("V-0001", 25), ("V-0002", 30)]
    assert ob.pending_pings() == 0


def test_add_gps_after_drops_already_released_pings() -> None:
    ob = Outbox()
    ob.add_gps(_batch("V-1", [10, 20, 30], 0), after_ms=20)
    _, gps = ob.release(100)
    assert [p.t_ms for p in gps] == [30]
