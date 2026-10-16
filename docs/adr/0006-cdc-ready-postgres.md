# ADR 0006: CDC-ready TMS Postgres (logical WAL, REPLICA IDENTITY FULL on orders and shipments)

**Status:** accepted (2026-10-16)

**Context.**
- The platform will run Debezium against TMS Postgres later. That is out of scope here, but the source must already support it, so the data and its history do not have to be rebuilt.
- Logical decoding requires `wal_level=logical`, which needs a restart to change.
- By default (`REPLICA IDENTITY DEFAULT`), UPDATE and DELETE change events carry only the primary key as the "before" image.
- The TMS updates `orders.status`, `shipments.current_status`, `attempt_count` and `updated_at` in place. Status-transition analysis (old → new) is a core platform exercise and needs full before-images.

**Decision.**
- `tms-postgres` starts with:
  - `wal_level=logical`
  - `max_replication_slots=10`
  - `max_wal_senders=10`
  - `max_slot_wal_keep_size=4GB`
- `max_slot_wal_keep_size` caps the WAL an abandoned slot can retain, which is the classic way a forgotten CDC slot fills the disk.
- `compose.yaml` is the single source of these values. CI applies the same values to its service container with `ALTER SYSTEM` plus a restart (`python -m tests.compose_info alter-system`). An integration test asserts that the live server matches.
- Migration 0001 sets `REPLICA IDENTITY FULL` on `orders` and `shipments` only, the tables updated in place. All other tables keep the default (primary key):
  - Master data changes rarely.
  - `trips` updates `actual_*` columns, but its before-image adds little; revisit if the platform needs it.
- No publication or slot is created here; the platform side owns those.

**Consequences.**
- UPDATEs on `orders` and `shipments` write the whole old row to WAL. At S/M scale (thousands of shipments per day, about 10 updates each), this is negligible.
- If a slot is abandoned for more than 4 GB of WAL, Postgres invalidates it. The consumer must then re-snapshot rather than the disk filling up.
- Ten slots and senders leave room for Debezium plus ad-hoc test consumers.
