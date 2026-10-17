"""Append-only JSON Lines ground truth, one file per topic per sim day, on its own volume.

Written before the fault injector, so it records what truly happened. `watermark()` (file
names and sizes after fsync) goes into each checkpoint; on resume `truncate_to()` cuts
everything written after that checkpoint, and deterministic replay writes it again byte for byte.

Completed days are sealed (gzipped) at checkpoints: `<day>.jsonl` -> `<day>.jsonl.gz`, written
to a temp file, fsynced, renamed, then the plain file removed. Gzip output is reproducible
(mtime 0, no embedded name). If a seal happened after the checkpoint being resumed, truncation
unpacks it again; a crash halfway through a seal is repaired the same way.
"""

from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path
from typing import IO

PLAIN, SEALED, TMP = ".jsonl", ".jsonl.gz", ".jsonl.gz.tmp"


class GroundTruthWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[Path, IO[str]] = {}
        self.lines_written = 0

    def _path(self, topic: str, day: str) -> Path:
        return self.run_dir / topic / f"{day}{PLAIN}"

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
        return sorted(p for p in self.run_dir.glob("*/*") if p.name.endswith((PLAIN, SEALED)))

    def watermark(self) -> dict[str, int]:
        self.sync()
        return {p.relative_to(self.run_dir).as_posix(): p.stat().st_size for p in self._files()}

    # --- sealing ---------------------------------------------------------------------------------
    def seal_before(self, day: str) -> list[str]:
        """Gzip every plain day file dated strictly before `day` (all of its events are out)."""
        sealed = []
        for p in sorted(self.run_dir.glob(f"*/*{PLAIN}")):
            if p.name[: -len(PLAIN)] >= day:
                continue
            fh = self._handles.pop(p, None)
            if fh is not None:
                fh.flush()
                os.fsync(fh.fileno())
                fh.close()
            gz = p.with_name(p.name + ".gz")
            tmp = p.with_name(p.name + ".gz.tmp")
            with p.open("rb") as src, tmp.open("wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zout:
                    shutil.copyfileobj(src, zout)
                raw.flush()
                os.fsync(raw.fileno())
            tmp.replace(gz)
            p.unlink()
            sealed.append(gz.relative_to(self.run_dir).as_posix())
        return sealed

    # --- resume ----------------------------------------------------------------------------------
    def truncate_to(self, watermark: dict[str, int]) -> None:
        self.close()
        for tmp in self.run_dir.glob(f"*/*{TMP}"):
            tmp.unlink()  # a seal interrupted before its rename
        for p in self._files():
            rel = p.relative_to(self.run_dir).as_posix()
            if rel in watermark:
                continue
            if rel.endswith(SEALED):
                plain_rel = rel[: -len(".gz")]
                plain = self.run_dir / plain_rel
                if plain_rel in watermark and not plain.exists():
                    with gzip.open(p, "rb") as src, plain.open("wb") as dst:  # undo a later seal
                        shutil.copyfileobj(src, dst)
            p.unlink()
        for rel, size in watermark.items():
            path = self.run_dir / rel
            if not path.exists():
                raise RuntimeError(f"ground truth file missing since the checkpoint: {rel}")
            if rel.endswith(PLAIN):
                with path.open("r+b") as fh:
                    fh.truncate(size)
            elif path.stat().st_size != size:
                raise RuntimeError(f"sealed ground truth file changed since the checkpoint: {rel}")

    # --- reading ---------------------------------------------------------------------------------
    def read_lines(self, topic: str) -> list[str]:
        self.sync()
        out: list[str] = []
        for p in sorted((self.run_dir / topic).glob("*"), key=lambda p: p.name.split(".")[0]):
            if p.name.endswith(SEALED):
                out.extend(gzip.decompress(p.read_bytes()).decode("utf-8").splitlines())
            elif p.name.endswith(PLAIN):
                out.extend(p.read_text(encoding="utf-8").splitlines())
        return out

    def close(self) -> None:
        for fh in self._handles.values():
            fh.close()
        self._handles = {}


def open_lines(path: Path) -> IO[str]:
    """Open a ground-truth day file (plain or sealed) for line reading."""
    if path.name.endswith(SEALED):
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")
