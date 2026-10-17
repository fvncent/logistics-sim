# CLAUDE.md: VietLink Express simulator

This repo is the **data source** (simulator) only. Never build any part of the data platform here.
Source of truth: `docs/SPEC.md`. If a request and the spec disagree, stop and ask. Do not invent facts the spec does not state.

## Scope
Phase 1 (MVP) only. Out of scope: weather, carrier files, invoice and COD flows (only the tables exist), a real fault injector (pass-through interface only), backfill, sharding, MinIO, Debezium, dbt, any UI.

## Hard rules
1. **Sim clock ≠ wall clock.** No code reads the real clock. Ruff TID251 bans `time.*` clocks and sleep and `datetime.now`/`today`. The single whitelisted file is `src/vlsim/engine/live.py`, the M6 pacer. TMS writes (`created_at`, `updated_at` and the like) take timestamps from the sim clock: never use Postgres `now()` or `server_default=func.now()`. Ruff cannot catch these, so review for them. Every event carries `event_time` from the sim clock with an explicit `+07:00` offset. Never stamp `ingest_time`.
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
`make up` (core: Postgres :$TMS_PG_PORT, default 55432, Redpanda :19092) · `make migrate` / `make migrate-down` · `make seed PRESET=s` · `make up-sim` · `make down` · `make lint` · `make test` · `make test-integration` · `make soak` · `make reconcile`

The host Postgres port is 55432 because a native Postgres may already hold 5432 on the dev Mac. Docker must be on PATH (`~/.docker/bin` on macOS). The Makefile keeps a macOS-only fallback with a warning; on Linux, docker targets fail if docker is missing. `make up-sim` fails until the engine exists (M3–M6). EXCEPTION is a terminal state (ADR 0005).

## Layout
`src/vlsim/{engine,world,models,tms,emitters,faults,validation}`, `config/`, `migrations/`, `docker/`, `tests/{unit,integration}`.

## Database
- `vlsim.tms.schema` (SQLAlchemy Core) is the schema; migrations are hand-reviewed and **frozen once merged**, so changes go in new revisions. A test checks that the migrations and the metadata do not drift.
- Never use server defaults or DB clock functions. `tests/unit/test_static_rules.py` scans the migrations, the TMS layer and the world builders.
- Only `vlsim.tms` imports SQLAlchemy. A test enforces that `engine`, `models`, `emitters`, `faults` and `world` never import the DB layer.
- `compose.yaml` is the single source of truth for the Postgres image and the CDC settings (ADR 0006). `tests/compose_info.py` reads it.
- RNG: `vlsim.engine.rng.stream(seed, name)` gives a named stream keyed by a SHA-256 of the name.

## Engine (M3)
- Time is int ms since the epoch (`vlsim.engine.clock`). Never use float times.
- Handlers are module-level functions registered by name: `handler(engine, event)`. They read `engine.clock` and use `engine.rng(name)`, `engine.schedule[_in](t, "name", payload)`, `engine.counters` and `engine.state` (plain data only).
- Checkpoints are canonical JSON in SQLite with a verified SHA-256 (ADR 0007). Anything that must survive a restart lives in `engine.state`, the queue, the RNG streams or the counters.
- `tests/unit/engine/test_determinism.py` pins golden hashes. Change them only on purpose, and say why in the commit.

## Business model (M4)
- Handlers live in `vlsim.models.flow`; their state layout is documented at the top of that file. `vlsim.models.sim.Simulation` runs them: it flushes and checkpoints on absolute time grids.
- **Never read mutable entity state from Postgres in a handler.** Use `engine.state` (checkpointed) and `ctx.facts` (immutable, re-read on restore). See ADR 0008.
- TMS writes are monotonic upserts. A shipment must never get two writes in the same sim millisecond: keep queue readiness checks strict (`<`).
- Iterate `engine.state` dicts in sorted order only; restored dicts come back sorted.
- `tests/fakes.InMemoryTms` mirrors `PostgresTms` semantics and validates every transition. Use it for fast tests.
- `vlsim simulate --preset s --days N` runs a batch against TMS Postgres and resumes from `--state-dir`.

## Emitters (M5)
- Path: handler → `ctx.outbox` → (after the TMS commit) `EmitPipeline`: ground truth → `FaultInjector` (pass-through) → `SpillingEmitter` → Redpanda. Never emit from a handler directly (ADR 0009).
- Scans: `SimEngine.emit_scan`, one per status write. GPS: `flow.start_gps` generates a trip's whole ping batch in one numpy call. The waypoints and seq base live in the trip state so a resume can regenerate them (ADR 0011).
- `config/emitters.yaml` is fingerprinted. `config/transport.yaml` (partitions, retention, producer, spill) is not (ADR 0010).
- `vlsim topics`, `vlsim simulate --run-id X`, `vlsim reconcile --run-id X` (or `make reconcile RUN=X`). Reconcile only after a clean stop.
- Integration tests marked `redpanda` need `make up`. The pause test runs `docker compose pause redpanda`.

## Operations (M6)
- Live: `vlsim run --speed N` (1–1000). `engine/live.py` holds the Pacer, WallClockFlush and LiveRunner, and is the only wall-clock reader. `vlsim.ops` holds metrics, health and the :8081 HTTP endpoint (`/metrics`, `/health`, `/speed`).
- SIGTERM leads to `Simulation.close()` and exit 0. Restarts resume from `--state-dir`. kill -9 is safe (replay).
- Ground-truth days before today are gzipped at checkpoints. `truncate_to` copes with seals, unseals and half-done seals.
- Reproducibility goldens are kept per `<OS>-<arch>` in `tests/golden/repro_hashes.json`. Regenerate with `uv run python -m tests.repro_run` only for intended model changes, and say so in the commit.
- `make soak` (`scripts/soak.sh`, in bash because it must sleep and read the clock) writes `data/soak/<run>/samples.csv`, `events.log` and `reconcile.json`.
- Unplanted baseline artifacts are listed in ASSUMPTIONS ("Known baseline artifacts"). Keep the list current when the model changes.

## Deployment
- Three Compose files: `compose.yaml` (base), `compose.server.yaml` (prod Redpanda, secrets) and `compose.server-data.yaml` (bind mounts), selected via `COMPOSE_FILE` in `.env` (ADR 0013). Never commit `.env`.
- `tests/unit/test_deploy_config.py` pins the invariants: restart policy, memory limits (sim-engine < 512 MB), log rotation, ports on `BIND_ADDR`, no secrets, durable Redpanda.
- `make soak SOAK_MODE=container` soaks the real server stack as the isolated project `vlsimsoak`. `make clean-scratch` drops `vlsim_*` databases.
- `docker kill` does not trigger restart policies. Crash tests use a host-side SIGKILL.
