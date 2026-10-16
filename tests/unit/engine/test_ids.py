from __future__ import annotations

import uuid

from vlsim.engine.ids import EmissionCounters


def test_counters_are_per_key_and_restorable() -> None:
    c = EmissionCounters()
    assert [c.next("scan"), c.next("scan"), c.next("gps")] == [1, 2, 1]
    r = EmissionCounters.restore(c.data())
    assert r.next("scan") == 3
    assert list(c.data()) == ["gps", "scan"]  # sorted


def test_event_id_is_deterministic_uuid5() -> None:
    c = EmissionCounters()
    a = c.event_id("tracking.scans", 7, {"shipment_id": "S-1", "event_type": "PICKED_UP"})
    b = c.event_id("tracking.scans", 7, {"event_type": "PICKED_UP", "shipment_id": "S-1"})
    assert a == b
    assert a.version == 5
    assert isinstance(a, uuid.UUID)
    assert a != c.event_id("tracking.scans", 8, {"shipment_id": "S-1", "event_type": "PICKED_UP"})
    assert a != c.event_id("telemetry.gps", 7, {"shipment_id": "S-1", "event_type": "PICKED_UP"})
