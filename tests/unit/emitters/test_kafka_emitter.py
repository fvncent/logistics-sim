from __future__ import annotations

from pathlib import Path

from tests.unit.emitters.fake_broker import FakeProducer
from vlsim.config import EmitterTuning
from vlsim.emitters.kafka import SpillingEmitter

TUNING = EmitterTuning(
    max_inflight=50, drain_batch=20, checkpoint_flush_timeout_s=0.01, max_spill_bytes=10**9
)


def _values(p: FakeProducer) -> list[bytes]:
    return [v for _t, _k, v in p.log]


def _emit_n(em: SpillingEmitter, start: int, n: int) -> list[bytes]:
    out = []
    for i in range(start, start + n):
        v = f'{{"i":{i}}}'.encode()
        em.emit("tracking.scans", f"S-{i:08d}", v)
        out.append(v)
        em.service()
    return out


def test_healthy_broker_delivers_everything_in_order(tmp_path: Path) -> None:
    p = FakeProducer()
    em = SpillingEmitter(p, tmp_path / "spill.jsonl", TUNING)
    sent = _emit_n(em, 0, 120)
    em.close()
    assert _values(p) == sent
    assert em.stats["spilled"] == 0


def test_outage_spills_to_disk_then_drains_without_loss(tmp_path: Path) -> None:
    p = FakeProducer(timeout_polls=2)
    em = SpillingEmitter(p, tmp_path / "spill.jsonl", TUNING)
    sent = _emit_n(em, 0, 30)
    p.down = True
    sent += _emit_n(em, 30, 200)  # far more than max_inflight while the broker is down
    assert em.stats["spilled"] > 0
    assert (tmp_path / "spill.jsonl").stat().st_size > 0
    p.down = False
    sent += _emit_n(em, 230, 30)
    em.close()
    assert set(_values(p)) == set(sent)  # nothing lost (duplicates allowed)
    assert em.backlog_bytes() == 0


def test_checkpoint_spills_unacked_and_survives_a_crash(tmp_path: Path) -> None:
    p = FakeProducer(timeout_polls=10**6)
    em = SpillingEmitter(p, tmp_path / "spill.jsonl", TUNING)
    p.down = True
    sent = _emit_n(em, 0, 40)
    state = em.checkpoint()  # broker down: unacked messages must be made durable
    assert em.backlog_bytes() > 0
    del em  # crash: in-memory queue gone with the process
    p2 = FakeProducer()
    em2 = SpillingEmitter(p2, tmp_path / "spill.jsonl", TUNING)
    em2.restore(state)
    em2.close()
    assert set(_values(p2)) == set(sent)


def test_backpressure_blocks_until_spill_drains(tmp_path: Path) -> None:
    tiny = TUNING.model_copy(update={"max_spill_bytes": 200})
    p = FakeProducer(timeout_polls=1)
    em = SpillingEmitter(p, tmp_path / "spill.jsonl", TUNING)
    p.down = True
    _emit_n(em, 0, 60)  # broker down: everything ends up in the spill
    em.tuning = tiny  # now the backlog is over the limit
    calls = {"n": 0}
    orig = p.poll

    def poll_and_recover(timeout: float = 0) -> int:
        calls["n"] += 1
        if calls["n"] > 5:
            p.down = False  # the broker comes back while the emitter is blocked
        return orig(timeout)

    p.poll = poll_and_recover  # type: ignore[method-assign]
    em.emit("tracking.scans", "S-x", b'{"late":1}')
    em.service()  # must block (poll) until the backlog is under the limit, not raise
    assert em.backlog_bytes() <= 200
    assert em.stats["backpressure_waits"] > 0
    em.close()
