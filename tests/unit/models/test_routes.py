from __future__ import annotations

import pytest

from vlsim.config import load_config
from vlsim.models.routes import Routes


@pytest.fixture(scope="module")
def routes() -> Routes:
    return Routes(load_config())


def _path(r: Routes, a: str, b: str) -> list[str]:
    path = [a]
    while path[-1] != b:
        path.append(r.next_hop(path[-1], b))
    return path


def test_paths_use_trunk_via_hubs(routes: Routes) -> None:
    assert _path(routes, "DP-HPH", "DP-VCA") == ["DP-HPH", "DP-HAN", "DP-DAD", "DP-SGN", "DP-VCA"]
    assert _path(routes, "DP-HAN", "DP-SGN") == ["DP-HAN", "DP-DAD", "DP-SGN"]  # direct lane unused
    assert _path(routes, "DP-HUI", "DP-DAD") == ["DP-HUI", "DP-DAD"]


def test_intermediate_nodes_are_hubs(routes: Routes) -> None:
    hubs = {"DP-HAN", "DP-DAD", "DP-SGN"}
    for a in routes.depot_ids:
        for b in routes.depot_ids:
            if a != b:
                assert set(_path(routes, a, b)[1:-1]) <= hubs


def test_distance_symmetric_and_circuity(routes: Routes) -> None:
    d = routes.distance_km("DP-HAN", "DP-DAD")
    assert d == routes.distance_km("DP-DAD", "DP-HAN")
    assert 700 < d < 900  # haversine ~630 km x 1.3


def test_distance_classes(routes: Routes) -> None:
    assert routes.distance_class("DP-HAN", "DP-HAN") == "same_depot"
    assert routes.distance_class("DP-HAN", "DP-HPH") == "same_region"
    assert routes.distance_class("DP-HAN", "DP-DAD") == "adjacent_region"
    assert routes.distance_class("DP-HAN", "DP-SGN") == "far_region"


def test_promise_is_local_evening_n_days_later(routes: Routes) -> None:
    created = 1_790_787_600_000 + 3_600_000 * 9  # 2026-10-01 09:00 +07:00
    promised = routes.promised_ms(created, "far_region", express=False)
    assert promised == 1_790_787_600_000 + 4 * 86_400_000 + 21 * 3_600_000
    assert routes.promised_ms(created, "far_region", express=True) == promised - 86_400_000
    assert routes.promised_ms(created, "same_depot", express=True) > created  # never < 1 day
