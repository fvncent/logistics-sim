"""Append-only JSON Lines ground truth, one file per topic per sim day, on its own volume.

Written before the fault injector, so it records what truly happened. `watermark()` (file
sizes after fsync) goes into each checkpoint; on resume `truncate_to()` cuts everything written
after that checkpoint, and deterministic replay writes it again byte for byte.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class GroundTruthWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[Path, IO[str]] = {}
        self.lines_written = 0

    def _path(self, topic: str, day: str) -> Path:
        return self.run_dir / topic / f"{day}.jsonl"

    def write(self, topic: str, day: str, line: str) -> None:
        path = self._path(topic, day)
        fh = self._handles.get(path)
        if fh is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = self._handles[path] = path.open("a", encoding="utf-8", newline="\n")
        fh.write(line)
        fh.write("\n")
        self.lines_written += 1

    def sync(self) -> None:
        for fh in self._handles.values():
            fh.flush()
            os.fsync(fh.fileno())

    def _files(self) -> list[Path]:
        return sorted(self.run_dir.glob("*/*.jsonl"))

    def watermark(self) -> dict[str, int]:
        self.sync()
        return {p.relative_to(self.run_dir).as_posix(): p.stat().st_size for p in self._files()}

    def truncate_to(self, watermark: dict[str, int]) -> None:
        self.close()
        for p in self._files():
            rel = p.relative_to(self.run_dir).as_posix()
            if rel not in watermark:
                p.unlink()
            else:
                with p.open("r+b") as fh:
                    fh.truncate(watermark[rel])
        missing = [rel for rel in watermark if not (self.run_dir / rel).exists()]
        if missing:
            raise RuntimeError(f"ground truth files missing since the checkpoint: {missing[:3]}")

    def read_lines(self, topic: str) -> list[str]:
        self.sync()
        out: list[str] = []
        for p in sorted((self.run_dir / topic).glob("*.jsonl")):
            out.extend(p.read_text(encoding="utf-8").splitlines())
        return out

    def close(self) -> None:
        for fh in self._handles.values():
            fh.close()
        self._handles = {}
