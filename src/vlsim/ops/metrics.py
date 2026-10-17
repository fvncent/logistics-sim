"""Prometheus metrics for the sim-engine (own registry; served at /metrics by OpsServer)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest


class SimMetrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.sim_time = Gauge("vlsim_sim_time_seconds", "Sim clock (unix seconds)", registry=r)
        self.events = Counter("vlsim_engine_events", "Engine events processed", registry=r)
        self.eps = Gauge("vlsim_events_per_second", "Engine events per wall second", registry=r)
        self.queue = Gauge("vlsim_queue_depth", "Pending events in the engine queue", registry=r)
        self.emitted = Counter(
            "vlsim_emitted_events", "Events released per topic", ["topic"], registry=r
        )
        self.spill = Gauge(
            "vlsim_spill_backlog_bytes", "Producer spill bytes not yet re-sent", registry=r
        )
        self.flush_lag_sim = Gauge(
            "vlsim_flush_lag_sim_seconds",
            "Sim time since the last TMS commit + release",
            registry=r,
        )
        self.flush_lag_wall = Gauge(
            "vlsim_flush_lag_wall_seconds",
            "Wall time since the last TMS commit + release",
            registry=r,
        )
        self.ckpt_sim = Gauge(
            "vlsim_checkpoint_age_sim_seconds", "Sim time since last checkpoint", registry=r
        )
        self.ckpt_wall = Gauge(
            "vlsim_checkpoint_age_wall_seconds", "Wall time since last checkpoint", registry=r
        )
        self.drift = Gauge(
            "vlsim_pacing_drift_seconds", "How far live pacing lags the wall clock", registry=r
        )
        self.speed = Gauge(
            "vlsim_speed", "Live speed knob (sim seconds per wall second)", registry=r
        )
        self._events_seen = 0
        self._emitted_seen: dict[str, int] = {}

    def update(
        self,
        *,
        sim_time_ms: int,
        events_total: int,
        queue_depth: int,
        emitted: dict[str, int],
        spill_backlog_bytes: int,
        flush_lag_sim_s: float,
        flush_lag_wall_s: float,
        checkpoint_age_sim_s: float,
        checkpoint_age_wall_s: float,
        drift_s: float,
        events_per_s: float,
        speed: float,
    ) -> None:
        self.sim_time.set(sim_time_ms / 1000)
        if events_total > self._events_seen:
            self.events.inc(events_total - self._events_seen)
            self._events_seen = events_total
        for topic, total in sorted(emitted.items()):
            seen = self._emitted_seen.get(topic, 0)
            if total > seen:
                self.emitted.labels(topic=topic).inc(total - seen)
                self._emitted_seen[topic] = total
        self.queue.set(queue_depth)
        self.spill.set(spill_backlog_bytes)
        self.flush_lag_sim.set(flush_lag_sim_s)
        self.flush_lag_wall.set(flush_lag_wall_s)
        self.ckpt_sim.set(checkpoint_age_sim_s)
        self.ckpt_wall.set(checkpoint_age_wall_s)
        self.drift.set(drift_s)
        self.eps.set(events_per_s)
        self.speed.set(speed)

    def exposition(self) -> bytes:
        out: bytes = generate_latest(self.registry)
        return out
