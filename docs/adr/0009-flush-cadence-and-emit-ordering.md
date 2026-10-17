# ADR 0009: Mode-dependent flush cadence; Postgres commits before topics see an event

**Status:** accepted (2026-10-17). Applies from M4. M5 attaches the emitters to it.

**Context.**
- TMS writes are write-behind (ADR 0008). In fast runs, a 15 sim-minute flush grid is efficient: at 1000x it is about 1 s of wall time.
- In live mode at 1x, the same grid would leave Postgres, and therefore CDC, up to 15 real minutes behind the simulation.
- Separately, the platform will join topic events (scans) to CDC rows. If a scan for status X reached Redpanda before the database committed X, consumers would see "future" events for rows that, in the database, don't exist yet or are still at an earlier status.

**Decision.**
1. **Cadence depends on the mode:**
   - *Fast/batch runs:* `SimTimeFlush`, every `runner.flush_interval_sim_minutes` (15) of sim time, plus before every checkpoint. It is deterministic and needs no wall clock.
   - *Live mode:* `vlsim.engine.live.WallClockFlush`, every `runner.live_flush_wall_seconds` (1.0) of wall time, plus before every checkpoint. `live.py` is the only module allowed to read the wall clock (ruff TID251). Wall time decides only *when* a flush happens, never *what* is simulated, so output stays deterministic.
2. **Order inside a flush** (`Simulation.flush_outputs`):
   1. Commit the buffered TMS writes in one Postgres transaction.
   2. Only after that commit, release the events produced since the last flush to ground truth, then the fault injector, then the producer (M5).

   An event is never handed to Kafka before the Postgres row it describes is committed. Topics may lag the database by up to one flush interval, but they never lead it.
3. A failed commit raises before anything is released. The events stay buffered and nothing is emitted for writes that were not committed.

**Consequences.**
- Live mode: Postgres is at most about 1 s plus one transaction behind sim time; topics are at most about one flush interval behind Postgres.
- Fast mode: events reach topics in bursts at every 15 sim-minute boundary. Ordering within a burst is by sim time.
- GPS pings belong to a trip, and the trip row is committed when the vehicle departs. They are released when their own `event_time` is ≤ the flush instant, so pings never arrive from the future (M5).
