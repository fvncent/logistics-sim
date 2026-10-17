"""Completed ground-truth days are gzipped; truncate-on-resume still works across sealing."""

from __future__ import annotations

from pathlib import Path

from vlsim.emitters.ground_truth import GroundTruthWriter

T = "telemetry.gps"


def _fill(gt: GroundTruthWriter) -> None:
    for day, n in (("2026-10-01", 3), ("2026-10-02", 2)):
        for i in range(n):
            gt.write(T, day, f'{{"d":"{day}","i":{i}}}')


def test_seal_completed_days_and_read_back(tmp_path: Path) -> None:
    gt = GroundTruthWriter(tmp_path)
    _fill(gt)
    sealed = gt.seal_before("2026-10-02")
    assert sealed == [f"{T}/2026-10-01.jsonl.gz"]
    assert not (tmp_path / T / "2026-10-01.jsonl").exists()
    assert (tmp_path / T / "2026-10-02.jsonl").exists()  # current day stays plain
    assert len(gt.read_lines(T)) == 5
    wm = gt.watermark()
    assert set(wm) == {f"{T}/2026-10-01.jsonl.gz", f"{T}/2026-10-02.jsonl"}
    gt.close()


def test_gzip_bytes_are_reproducible(tmp_path: Path) -> None:
    for d in ("a", "b"):
        gt = GroundTruthWriter(tmp_path / d)
        _fill(gt)
        gt.seal_before("2026-10-02")
        gt.close()
    f = f"{T}/2026-10-01.jsonl.gz"
    assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()


def test_truncate_undoes_a_seal_made_after_the_checkpoint(tmp_path: Path) -> None:
    gt = GroundTruthWriter(tmp_path)
    gt.write(T, "2026-10-01", "a")
    wm = gt.watermark()  # checkpoint: day 1 still open with 1 line
    gt.write(T, "2026-10-01", "b")
    gt.write(T, "2026-10-02", "c")
    gt.seal_before("2026-10-02")  # day 1 sealed after the checkpoint, then crash
    gt.close()
    gt2 = GroundTruthWriter(tmp_path)
    gt2.truncate_to(wm)
    assert gt2.read_lines(T) == ["a"]
    assert (tmp_path / T / "2026-10-01.jsonl").exists()
    assert not (tmp_path / T / "2026-10-01.jsonl.gz").exists()
    assert not (tmp_path / T / "2026-10-02.jsonl").exists()
    gt2.close()


def test_sealed_files_in_the_watermark_are_kept(tmp_path: Path) -> None:
    gt = GroundTruthWriter(tmp_path)
    _fill(gt)
    gt.seal_before("2026-10-02")
    wm = gt.watermark()
    gt.write(T, "2026-10-02", "late")
    gt.close()
    gt2 = GroundTruthWriter(tmp_path)
    gt2.truncate_to(wm)
    assert len(gt2.read_lines(T)) == 5
    gt2.close()
