# Benchmarks

Measured, not estimated. Machine: the development MacBook (Apple Silicon, arm64), with Docker Desktop Postgres 17.11.

| Date | What | Result |
|---|---|---|
| 2026-10-16 | Bare engine loop (1 handler, 1,000 pending events) | ~445k events/s; building a checkpoint takes ~3 ms |
| 2026-10-17 | **Full S-scale week**, `vlsim simulate --preset s --days 7`, fresh DB, timed with `/usr/bin/time` | 24,769 events and 98,458 TMS rows in **22.6 s wall** ≈ **1,100 events/s** (≈ 4,400 rows/s). Peak RSS 118 MB. |
| 2026-10-17 | Same week with the in-memory TMS (no Postgres) | 1.5 s wall including ~0.7 s of imports ≈ 30k events/s of business logic. Postgres writes are ~90% of the full run. |

| 2026-10-17 | **Full S-scale week including GPS and Redpanda** (M5): `vlsim simulate --preset s --days 7` on a fresh DB, emitting to ground truth and Redpanda, timed with `/usr/bin/time` | 24,769 engine events, **1,012,454 emitted events** (57,114 scans + 955,340 GPS pings) and 98,458 TMS rows in **57.8 s wall** ≈ **17.5k emitted events/s** (≈ 17.9k/s counting engine events too). Peak RSS 138 MB. Ground truth is 218 MB. `vlsim reconcile`: all 1,012,454 matched, 0 status mismatches. |
| 2026-10-17 | Redpanda paused mid-run (M5 test, 2 sim days) | ~158k messages went through the disk spill and were all re-delivered: 155,920 events reconciled with 0 missing. |

S scale produces about 3.5k engine events per sim day (GPS pings are not engine events). At 1000x live speed that is about 40 engine events/s. Including GPS, S emits about 145k events per sim day: about 1.7 events/s at 1x, and about 1,700/s at 1000x, about 10% of the measured 17.5k/s ceiling. The emit path costs about 35 µs per event (JSON, UUIDv5, ground-truth write, produce). Profile it before trying M at 1000x. Not measured yet: M scale, and the amd64 Ubuntu server.

## On-time analysis: why same-region (73%) trails far-region (86%) (M4 follow-up, 2026-10-17)

The figures cover shipments created on days 1–2 and delivered within 9 sim days (S preset, seed 20261016, in-memory TMS; the model is identical against Postgres). Nothing has been tuned. Calibration is Phase 2.

| Class | n | On-time | Standard | Express | Median transit | Median time to promise |
|---|---|---|---|---|---|---|
| same depot | 302 | 91% | 89% | 96% | 16 h | 29 h |
| same region | 490 | **73%** | 83% | **32%** | 40 h | 51 h |
| adjacent region | 1041 | 83% | 88% | 50% | 58 h | 76 h |
| far region | 517 | 86% | 90% | 54% | 80 h | 100 h |

By number of line-haul legs: same region with 1 leg is 77% on time, with 2 legs 60%. Far region with 2 legs is 92%, 3 legs 83%, 4 legs 75%.

**Causes, largest first:**
1. **Express takes a whole day off a 2-day SLA.**
   - `express_days_saved: 1` turns the same-region promise into "tomorrow 21:00", which these paths can't meet: pickup run → dock → sort → a feeder with two departures a day → sort → next rider shift. Median transit is 37 h for one leg.
   - Only 32% of same-region express shipments make it, against 54% for far-region express, where 3 days remain.
   - Same region also has the higher express share (18% vs 11%, A30), so it carries more of these impossible promises. Excluding express, the gap shrinks to 83% vs 90%.
2. **The SLA is set per region, not by distance or number of legs.**
   - "Same region" covers 1-leg feeder hops of 100–570 km (HPH–HAN 2.6 h of driving, NHA–DAD about 11 h) and 2-leg hub transfers (HPH→HAN→VII, HUI→DAD→NHA). All of them get the same 2 days.
   - Each extra leg adds a discrete half-day-or-more wait for the next feeder slot, which uses up a much larger share of a 2-day promise than of a 4-day one.
   - The far region rides the overnight trunk (22:00 departures) and has 100 h of promise against an 80 h median transit.
3. **Feeder contention at depots that have two feeder lanes and one van.**
   - VII (feeding both HAN and DAD) shares one van between the lanes. With the return-home rule, a lane often waits a day for the van, so VII→HAN is only 23% on time.
   - NHA (feeding DAD and SGN) depends on hub vans coming in. NHA→DAD is 61% on time and NHA→HUI 60%.

**Levers for Phase 2 calibration (not applied):**
- Make SLA days depend on path legs or distance, and/or make the express saving class-dependent.
- Add a third feeder departure, or a second van at VII/NHA.
- Add a 13:00 pickup run.
