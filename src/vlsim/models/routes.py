"""Network geometry: road distances, next-hop routing over line-haul lanes, SLA promises."""

from __future__ import annotations

import heapq
import math

from vlsim.config import DistanceClass, LaneKind, SimConfig
from vlsim.engine.clock import DAY_MS, local_midnight_ms, time_of_day_ms
from vlsim.world.build import depot_id

EARTH_RADIUS_KM = 6371.0088
ROUTED_LANE_KINDS: tuple[LaneKind, ...] = ("main", "feeder")  # "direct" exists but is unused
REGION_ORDER = {"north": 0, "central": 1, "south": 2}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class Routes:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.circuity = cfg.base.distance.circuity_factor
        self.offset_minutes = _offset_minutes(cfg.base.timezone_offset)
        depots = cfg.network.depots
        self.depot_ids: tuple[str, ...] = tuple(depot_id(d.code) for d in depots)
        self.coords = {depot_id(d.code): (d.lat, d.lon) for d in depots}
        self.region = {depot_id(d.code): d.region for d in depots}
        self.is_hub = {depot_id(d.code): d.type == "hub" for d in depots}
        self.lane_kind: dict[tuple[str, str], LaneKind] = {}
        for lane in cfg.network.lanes:
            a, b = depot_id(lane.a), depot_id(lane.b)
            self.lane_kind[(a, b)] = self.lane_kind[(b, a)] = lane.kind
        self._next = {dest: self._shortest_tree(dest) for dest in self.depot_ids}

    def distance_km(self, a: str, b: str) -> float:
        (la, oa), (lb, ob) = self.coords[a], self.coords[b]
        return haversine_km(la, oa, lb, ob) * self.circuity

    def point_distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        return haversine_km(lat1, lon1, lat2, lon2) * self.circuity

    def directed_lanes(self) -> list[tuple[str, str, LaneKind]]:
        """Routed lanes in both directions, in a fixed order."""
        return sorted((a, b, k) for (a, b), k in self.lane_kind.items() if k in ROUTED_LANE_KINDS)

    def next_hop(self, at: str, dest: str) -> str:
        if at == dest:
            raise ValueError("already at destination")
        return self._next[dest][at]

    def _shortest_tree(self, dest: str) -> dict[str, str]:
        """Dijkstra towards `dest`; ties broken by depot id so routing is deterministic."""
        dist = {dest: 0.0}
        nxt: dict[str, str] = {}
        heap: list[tuple[float, str]] = [(0.0, dest)]
        done: set[str] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            for (a, b), kind in sorted(self.lane_kind.items()):
                if b != node or kind not in ROUTED_LANE_KINDS:
                    continue
                cand = d + self.distance_km(a, b)
                if cand < dist.get(a, math.inf) - 1e-9:
                    dist[a], nxt[a] = cand, node
                    heapq.heappush(heap, (cand, a))
        missing = set(self.depot_ids) - done
        if missing:
            raise ValueError(f"depots {sorted(missing)} cannot reach {dest}")
        return nxt

    def distance_class(self, origin: str, dest: str) -> DistanceClass:
        if origin == dest:
            return "same_depot"
        gap = abs(REGION_ORDER[self.region[origin]] - REGION_ORDER[self.region[dest]])
        return ("same_region", "adjacent_region", "far_region")[gap]

    def promised_ms(self, created_ms: int, cls: DistanceClass, *, express: bool) -> int:
        sla = self.cfg.operations.sla
        days = max(1, sla.days[cls] - (sla.express_days_saved if express else 0))
        t = sla.promise_local_time
        day0 = local_midnight_ms(created_ms, self.offset_minutes)
        return day0 + days * DAY_MS + time_of_day_ms(t.hour, t.minute)


def _offset_minutes(offset: str) -> int:
    sign = 1 if offset[0] == "+" else -1
    hh, mm = offset[1:].split(":")
    return sign * (int(hh) * 60 + int(mm))
