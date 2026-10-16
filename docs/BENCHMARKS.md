# Benchmarks

Measured, not estimated. Machine: the development MacBook (Apple Silicon, arm64), with Docker Desktop Postgres 17.11.

| Date | What | Result |
|---|---|---|
| 2026-10-16 | Bare engine loop (1 handler, 1,000 pending events) | ~445k events/s; building a checkpoint takes ~3 ms |
| 2026-10-17 | **Full S-scale week**, `vlsim simulate --preset s --days 7`, fresh DB, timed with `/usr/bin/time` | 24,769 events and 98,458 TMS rows in **22.6 s wall** ≈ **1,100 events/s** (≈ 4,400 rows/s). Peak RSS 118 MB. |
| 2026-10-17 | Same week with the in-memory TMS (no Postgres) | 1.5 s wall including ~0.7 s of imports ≈ 30k events/s of business logic. Postgres writes are ~90% of the full run. |

S scale produces about 3.5k engine events per sim day (GPS pings are not engine events). At 1000x live speed that is about 40 events/s, well within budget. Not measured yet: M scale, and the amd64 Ubuntu server.
