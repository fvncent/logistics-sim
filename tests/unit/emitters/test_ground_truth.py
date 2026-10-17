from __future__ import annotations

from pathlib import Path

from vlsim.emitters.ground_truth import GroundTruthWriter


def test_write_watermark_truncate(tmp_path: Path) -> None:
    gt = GroundTruthWriter(tmp_path / "run")
    gt.write("tracking.scans", "2026-10-01", '{"a":1}')
    gt.write("telemetry.gps", "2026-10-01", '{"g":1}')
    gt.sync()
    wm = gt.watermark()
    gt.write("tracking.scans", "2026-10-01", '{"a":2}')
    gt.write("tracking.scans", "2026-10-02", '{"a":3}')
    gt.sync()
    gt.close()

    gt2 = GroundTruthWriter(tmp_path / "run")
    gt2.truncate_to(wm)
    assert gt2.read_lines("tracking.scans") == ['{"a":1}']
    assert not (tmp_path / "run" / "tracking.scans" / "2026-10-02.jsonl").exists()
    gt2.write("tracking.scans", "2026-10-01", '{"a":2b}')
    gt2.sync()
    assert gt2.read_lines("tracking.scans") == ['{"a":1}', '{"a":2b}']
    assert gt2.read_lines("telemetry.gps") == ['{"g":1}']
    gt2.close()


def test_empty_watermark_clears_run(tmp_path: Path) -> None:
    gt = GroundTruthWriter(tmp_path / "run")
    gt.write("tracking.scans", "2026-10-01", "x")
    gt.sync()
    gt.truncate_to({})
    assert gt.read_lines("tracking.scans") == []
    gt.close()
