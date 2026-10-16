# ADR 0002: Simulator reaches the TMS through an in-process protocol

**Status:** accepted (2026-10-16)

**Context.** The TMS service must own every business write to Postgres so that CDC later sees real transactional updates. The simulator must not write SQL. HTTP between the two would add latency and failure modes that the MVP does not need yet.

**Decision.**
- `vlsim.tms` exposes a `TmsService` `typing.Protocol` with business verbs, for example `create_order` and `record_scan_transition`.
- `sim-engine` calls an in-process implementation backed by SQLAlchemy. The engine code (`vlsim.engine`, `vlsim.models`, `vlsim.emitters`) imports only the protocol and plain DTOs, **never** SQLAlchemy models. A test will enforce this import boundary.
- The `tms-service` container runs the same package as a FastAPI app with `/health` and read-only endpoints.

**Consequences.**
- Moving to HTTP later only needs a second `TmsService` implementation (an HTTP client).
- The in-process path means the engine container holds DB credentials.
