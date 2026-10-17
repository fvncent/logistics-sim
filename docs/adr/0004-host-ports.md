# ADR 0004: Non-default host port for TMS Postgres

**Status:** accepted (2026-10-16)

**Context.** The development Mac runs a native Postgres on 127.0.0.1:5432. It silently shadowed the container port, and host clients connected to the wrong server.

**Decision.** Map TMS Postgres to host port **55432** (override with `TMS_PG_PORT`). Inside the Compose network it stays `tms-postgres:5432`. Redpanda's external listener is `127.0.0.1:19092`.

**Consequences.** The default DSN is `localhost:55432`. CI uses its own service container and sets `VLSIM_TMS_DSN` explicitly.

**Update (2026-10-17, deploy):** Kafka clients and Redpanda's advertised external address use the IPv4 literal `127.0.0.1`, never the name `localhost`. Ports are published on `127.0.0.1` only (IPv4), but Ubuntu resolves `localhost` to `::1` first. librdkafka then gets "connection refused" on IPv6 and, because the broker also advertised `localhost`, fails with `_DESTROY_BROKER` (seen in CI on the deploy-server PR). Postgres is unaffected because libpq tries every resolved address.
