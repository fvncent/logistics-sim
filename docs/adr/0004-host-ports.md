# ADR 0004: Non-default host port for TMS Postgres

**Status:** accepted (2026-10-16)

**Context.** The development Mac runs a native Postgres on 127.0.0.1:5432. It silently shadowed the container port, and host clients connected to the wrong server.

**Decision.** Map TMS Postgres to host port **55432** (override with `TMS_PG_PORT`). Inside the Compose network it stays `tms-postgres:5432`. Redpanda's external listener is `localhost:19092`.

**Consequences.** The default DSN is `localhost:55432`. CI uses its own service container and sets `VLSIM_TMS_DSN` explicitly.
