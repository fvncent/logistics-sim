"""Named, independent RNG streams.

Each component draws from its own stream, keyed by the global seed plus a stable hash of the
stream name (not by spawn order), so adding a stream never changes the draws of another.
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np


def name_key(name: str) -> int:
    """Stable 64-bit key for a stream name (first 8 bytes of SHA-256, big-endian)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def stream(seed: int, name: str) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, name_key(name)])))


class RngStreams:
    """Lazily created named streams whose exact state can be captured and restored."""

    __slots__ = ("_streams", "seed")

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._streams: dict[str, np.random.Generator] = {}

    def get(self, name: str) -> np.random.Generator:
        gen = self._streams.get(name)
        if gen is None:
            gen = self._streams[name] = stream(self.seed, name)
        return gen

    def state(self) -> dict[str, dict[str, Any]]:
        """bit_generator.state of every stream, keyed by name in sorted order."""
        return {n: dict(self._streams[n].bit_generator.state) for n in sorted(self._streams)}

    @classmethod
    def restore(cls, seed: int, states: dict[str, dict[str, Any]]) -> RngStreams:
        s = cls(seed)
        for name in sorted(states):
            bitgen = np.random.PCG64()
            bitgen.state = states[name]  # type: ignore[assignment]  # numpy's TypedDict
            s._streams[name] = np.random.Generator(bitgen)
        return s
