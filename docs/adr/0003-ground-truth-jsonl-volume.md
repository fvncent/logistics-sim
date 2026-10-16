# ADR 0003: Ground truth as append-only JSONL on a dedicated volume

**Status:** accepted (2026-10-16)

**Context.** The spec places ground truth in a separate MinIO bucket with its own credentials. MinIO is out of scope for the MVP.

**Decision.**
- Write ground truth as append-only JSON Lines files on the `groundtruth` Docker volume.
- Mount that volume only into `sim-engine`.
- Write each record before the (pass-through) fault injector runs.
- Never store ground truth in TMS Postgres.

**Consequences.**
- The platform cannot reach the volume, which preserves the "sealed bucket" property.
- Migrating to MinIO later is a writer swap plus an upload of the existing files.
