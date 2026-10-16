# ADR 0007: Engine core, plain-data events and canonical checkpoints

**Status:** accepted (2026-10-16)

**Context.** Same seed + config must give byte-identical output, and a restart must resume rather than reset (SPEC). Both require that every piece of engine state is exact and serializable, and that nothing depends on wall time, hash randomization or object identity.

**Decision.**

*Time*
- Sim time is `int` milliseconds since the Unix epoch (UTC).
- `event_time` strings render it with millisecond precision and the explicit offset, e.g. `2026-10-16T09:41:12.000+07:00`.
- Floats are rejected anywhere a time is expected.

*Events*
- An event is `Event(time_ms, seq, handler_name, payload)`.
- The payload is deep-copied into JSON plain data. Callables, sets, non-str keys and NaN are rejected.
- The heap orders events by `(time_ms, seq)`. `seq` is a global monotonic counter, so same-instant events run in the order they were scheduled (FIFO).
- Handlers are registered by name and must be module-level functions; lambdas and closures are rejected. Scheduling an unknown handler or a time in the past fails immediately.

*Random numbers*
- Streams are named and keyed by seed + SHA-256(name), created lazily. Each stream's `bit_generator.state` is checkpointed.

*Event IDs*
- `EmissionCounters` keep a per-kind counter (kind = a logical event family, such as a topic).
- `event_id = uuid5(NS, canonical_json([kind, n, content]))`. A fault duplicate reuses the original ID.

*Checkpoints*
- Canonical JSON: sorted keys, compact separators, UTF-8, no NaN, no pickle.
- A checkpoint holds the format, config fingerprint, seed, clock, `next_seq`, `events_processed`, the queue in pop order, RNG states, counters and the world state (`engine.state`, plain data).
- Checkpoints are stored in an append-only SQLite table with their SHA-256, which is verified on load. The newest `keep` checkpoints are retained.
- WAL journal plus `synchronous=FULL`.
- Restoring refuses a different config fingerprint or a handler name the code no longer registers.

*Checkpoint timing*
- Checkpoints are taken only between events, never inside a handler. `run(until_ms)` leaves the clock at `until_ms`.

**Consequences.**
- Emitted output (topics, ground truth, TMS writes) is not part of the checkpoint.
  - After a **graceful** stop, output and checkpoint are consistent.
  - After a **crash**, the engine resumes from the last checkpoint and re-emits what happened since then. TMS writes must therefore be idempotent upserts (M4). Duplicates on topics carry identical `event_id`s and are deduplicated by reconcile (M5).
  - The ground-truth writer must truncate to the checkpoint watermark on resume (M5).
- Handler names are a persisted contract: renaming one invalidates old checkpoints, by design.
- A pinned golden hash of a toy chain guards against accidental changes to RNG, ordering, IDs or serialization. Because CI runs on x86-64, it also checks cross-architecture determinism for that chain.
