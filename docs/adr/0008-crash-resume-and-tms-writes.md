# ADR 0008: Crash-resume via deterministic replay and monotonic TMS upserts

**Status:** accepted (2026-10-17)

**Context.**
- A crash between checkpoints must not corrupt or diverge the TMS database. After a resume, Postgres must end up identical to an uninterrupted run.
- Postgres is the source of truth for entities, and checkpoints should stay small.
- Checkpoints (SQLite) and TMS writes (Postgres) cannot share one atomic transaction.

**Decision.**
1. **Handlers never read mutable entity state from Postgres.** At startup the TMS reads master data (static), and on restore it reads the immutable facts of in-flight shipments. Nothing else is read.
   - Handler inputs are therefore identical in a replay, whatever the database already contains.
   - The facts are converted to exact integers (`weight_g`, `cod_vnd`, epoch ms), so cached and re-read values are bit-identical.
2. **The checkpoint holds only operational state:**
   - id counters;
   - vehicle and driver positions and availability;
   - dock queues;
   - per-depot queues of `[shipment_id, ready_ms, attempts]`;
   - in-flight trips.

   Entity attributes are not stored there. On restore, the engine re-reads facts from Postgres for exactly the shipment ids the checkpoint references.
3. **TMS writes are write-behind and idempotent.** They are buffered and applied in one transaction per flush, every 15 sim-minutes, before every checkpoint, and when the buffer reaches 50k rows. Every write is an upsert keyed by a deterministic id:
   - **shipments:** mutable columns change only if `excluded.updated_at > shipments.updated_at` (sim time).
   - **orders:** status only moves forward (CREATED < IN_FULFILLMENT < terminal).
   - **trips:** `actual_dep` and `actual_arr` are filled once and never cleared.
   - **trip_shipments and cod_collections:** insert-only.

   Several writes to the same row in one flush run as ordered "waves" (one write per key per statement). CDC therefore sees every transition in order.
4. **Ordering at a boundary:** run to the boundary, flush, then save the checkpoint. Postgres is never behind a saved checkpoint; after a crash it can only be ahead.
5. **One write per shipment per sim millisecond.** Queues hand parcels on strictly after their `ready_ms`, so two transitions of one shipment never share an `updated_at`. Without this, the `<` guard could drop one of them. The in-memory test TMS asserts the rule.

**Crash cases.**
- *Crash before a flush commits:* Postgres matches the previous flush. Replay regenerates the lost writes.
- *Crash after post-checkpoint flushes:* Postgres is ahead. Replay re-sends older writes and the guards skip them, so rows never move backwards and no CDC events are emitted. Writes the database never saw are applied.
- *Crash during a flush:* the transaction rolls back, which is the first case.

**Verification.** Both an in-memory TMS and real Postgres run three days uninterrupted and also "crash" after 9 h of flushed post-checkpoint writes plus 2 h of discarded buffered writes, then resume. Each test asserts that every row of every business table is identical and that the final checkpoint hash is identical.

**Consequences.**
- Postgres lags sim time by up to one flush interval: 15 sim-minutes, which is 15 real minutes at 1x speed. M6 may add a wall-time flush in live mode.
- Replay after a crash re-emits writes. Postgres skips them, but M5's topic emitters will re-send events with identical `event_id`s, and reconcile deduplicates them.
- Adding a mutable column to `shipments` means adding it to `SHIPMENT_MUTABLE`. Making EXCEPTION recoverable (Phase 3) does not break the guards.
