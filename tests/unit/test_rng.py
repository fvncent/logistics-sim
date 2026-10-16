from __future__ import annotations

import numpy as np

from vlsim.engine.rng import name_key, stream


def test_same_seed_and_name_give_same_draws() -> None:
    assert np.array_equal(
        stream(7, "world.merchants").random(5), stream(7, "world.merchants").random(5)
    )


def test_streams_independent_of_creation_order() -> None:
    a_first = stream(7, "a").random(3)
    stream(7, "b").random(3)
    b_then = stream(7, "b").random(3)
    stream(7, "a")  # creating more streams must not shift existing ones
    assert np.array_equal(stream(7, "a").random(3), a_first)
    assert np.array_equal(stream(7, "b").random(3), b_then)


def test_different_names_and_seeds_differ() -> None:
    assert not np.array_equal(stream(7, "a").random(3), stream(7, "b").random(3))
    assert not np.array_equal(stream(7, "a").random(3), stream(8, "a").random(3))


def test_name_key_is_stable_across_processes() -> None:
    # Pinned value: a change here silently reshuffles every stream. Python's hash() would fail this.
    assert name_key("world.merchants") == 0x8350165515E933B9
