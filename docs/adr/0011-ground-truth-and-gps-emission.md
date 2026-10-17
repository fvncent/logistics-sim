# ADR 0011: Ground truth, scans and vectorized GPS emission

**Status:** accepted (2026-10-17)

**Decision.**

*Scans*
- Every shipment status write emits exactly one `tracking.scans` event, CREATED included. The event carries:
  - `schema_v`, `event_type` (= the new status), `depot` (code), `event_time` (+07:00, ms);
  - `device_id` (depot scanner, pickup handheld, the merchant API, or the rider's handheld);
  - `driver_id` (rider or line-haul driver, else null);
  - `reason_code`: EXCEPTION is `damage`, `lost` or `address_unresolvable`; DELIVERY_FAILED is `recipient_unavailable`, `address_issue` or `cod_refused` (taken from the same random draw that decided the failure, so no extra randomness); RETURNED is `max_attempts`.
- `event_id` = UUIDv5 over (topic, the per-topic counter, content). The counter is checkpointed.

*GPS*
- When a trip starts, the trip's entire ping series is generated in **one numpy batch**: one ping every 10 s, strictly after departure, up to arrival. It uses the trip's own random stream (`seed` + `"gps.<trip_id>"`).
- Paths are linear in time: depot → depot for line-haul; depot → each stop (arrive, then dwell for the service time) → depot for a rider loop. The batch adds receiver noise (8 m), road speed with noise, and fuel falling with road distance.
- `device_seq` is a per-vehicle counter. The trip reserves its range up front (`EmissionCounters.take`), so seq is contiguous per vehicle. `event_id` = UUIDv5 over (`telemetry.gps:<vehicle>`, `device_seq`, content).
- Pings sit in the outbox and are released only once their `event_time` is at or before the flush instant, so no event comes from the future. Release order is (time, vehicle, seq) and does not depend on flush cadence.
- Only the trip's waypoints and seq base are checkpointed. On resume, the in-flight trips' batches are regenerated and pings at or before the checkpoint instant are dropped.

*Ground truth*
- `<VLSIM_GROUND_TRUTH_DIR>/<run_id>/<topic>/<sim-date>.jsonl` sits on the `groundtruth` volume, which is mounted only into `sim-engine`.
- Each event is written **before** the fault injector (pass-through in the MVP), as the exact canonical JSON sent to Redpanda.
- At each checkpoint the files are fsynced and their sizes recorded (the watermark). On resume, files are truncated to the watermark and newer files deleted; replay then rewrites the same bytes. A crash-and-resume run produces ground truth byte-identical to an uninterrupted run (tested).

*Release pipeline* (ADR 0009): commit Postgres → outbox → ground truth → injector → producer.

**Consequences.**
- Ground truth equals what the platform should have received before faults, so reconcile can measure faults exactly (v2+).
- GPS volume follows real activity: vehicles only ping while on a trip. Measured at S: about 136k pings per sim day, against the SPEC's estimate of about 220k, which assumed every vehicle moving 12 h a day.
- Ground truth uses about 31 MB of disk per sim day at S (about 215 MB per week).
