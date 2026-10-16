"""Named, independent RNG streams.

Each component draws from its own stream, keyed by the global seed plus a stable hash of the
stream name (not by spawn order), so adding a stream never changes the draws of another.
"""

from __future__ import annotations

import hashlib

import numpy as np


def name_key(name: str) -> int:
    """Stable 64-bit key for a stream name (first 8 bytes of SHA-256, big-endian)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def stream(seed: int, name: str) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, name_key(name)])))
