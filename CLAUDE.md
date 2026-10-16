# CLAUDE.md: VietLink Express simulator

This repo is the **data source** (simulator) only. Never build any part of the data platform here.
Source of truth: `docs/SPEC.md`. If a request and the spec disagree, stop and ask. Do not invent facts the spec does not state.

## Scope
Phase 1 (MVP) only. Out of scope: weather, carrier files, invoice and COD flows (only the tables exist), a real fault injector (pass-through interface only), backfill, sharding, MinIO, Debezium, dbt, any UI.

## Hard rules
1. **Sim clock ≠ wall clock.** Handlers never call `datetime.now` or `time.time` (ruff TID251 bans them; the only exception is the live-mode pacer in `vlsim.engine`). Every event carries `event_time` from the sim clock with an explicit `+07:00` offset. Never stamp `ingest_time`.
2. **Determinism.** Same seed + config ⇒ byte-identical output.
   - heapq ordered by `(time, seq)` with a monotonic sequence number.
   - Every component has its own RNG stream, derived from `seed` + a **stable hash of the stream name** (not spawn order).
   - Never iterate unordered sets or dicts where order affects output.
3. **Config-driven.** Presets, depots, lanes, seasonality and rates live in versioned YAML under `config/`. No magic numbers in code.
4. **Money is integer VND.** No floats for currency.
5. **Ground truth** is written *before* any fault is applied, to append-only JSONL in the `groundtruth` volume. It is never written to TMS Postgres.
6. **TMS service owns all business writes** (it updates `status`, `updated_at` and `attempt_count` in place for CDC). `sim-engine` talks to it through the `TmsService` protocol and **must never import SQLAlchemy models or write SQL**.
7. **Emitters** batch and apply backpressure: they slow down or buffer to disk, and never crash or drop silently.
8. **Event schemas** follow the spec:
   - `tracking.scans` is keyed by `shipment_id`; `telemetry.gps` is keyed by `vehicle_id` and carries `device_seq`.
   - Every event has `schema_v`.
   - `event_id` = UUIDv5(content + a **per-logical-event counter stored in checkpoints**). Fault duplicates reuse the original `event_id`.
9. **GPS** for a whole trip is generated in one vectorized numpy batch.
10. **Transitions** are driven by queues, cutoffs, shift limits and capacity, not coin flips alone. Dock doors are a queue.
11. **Checkpoints** store events as plain data with handler *names*, never closures.
12. Keep it simple: no premature abstraction.

## Process
- Work one milestone at a time (M1–M6), each on its own branch, merged into `main` after CI is green. Stop and report after each milestone.
- Write tests first for engine and state-machine code. Make small commits.
- Record modelling assumptions in `docs/ASSUMPTIONS.md` and non-obvious decisions as ADRs in `docs/adr/`.
- Ask before adding any dependency. Pin versions. Images must be multi-arch (amd64 + arm64).
- Be honest about anything that could not be verified.

## Commands
`make up` (core: Postgres :55432, Redpanda :19092) · `make up-sim` · `make down` · `make lint` · `make test` · `make test-integration` · `make soak` · `make reconcile`

The host Postgres port is 55432 because a native Postgres may already hold 5432 on the dev Mac. The Makefile finds the Docker Desktop CLI even when it is not on PATH.

## Layout
`src/vlsim/{engine,world,models,tms,emitters,faults,validation}`, `config/`, `migrations/` (M2), `docker/`, `tests/{unit,integration}`.
