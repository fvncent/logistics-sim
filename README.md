# VietLink Express simulator

A 24/7 discrete-event simulator that plays the source systems of a fictional Vietnamese
logistics company (TMS Postgres, Redpanda scan and GPS topics) for a separate data platform.
See [docs/SPEC.md](docs/SPEC.md). Full run instructions and an architecture diagram arrive in M6.

## Quick start (development)

```bash
make up      # Postgres + Redpanda (Compose profile "core")
make migrate # apply TMS schema (Alembic)
make seed    # deterministic, idempotent master data (PRESET=s|m)
make topics  # create Redpanda topics (6 partitions, 7-day retention)
uv run vlsim simulate --days 7 --run-id demo   # batch run: Postgres + ground truth + Redpanda
make reconcile RUN=demo                         # ground truth vs topics vs Postgres
make lint
make test
make down
```

`make up` exposes TMS Postgres on `localhost:${TMS_PG_PORT:-55432}` and Redpanda on
`localhost:19092`. Copy `.env.example` to `.env` to change ports. Docker must be on PATH; with
Docker Desktop on macOS that means `~/.docker/bin`.

> **Known limitation until M3-M6:** `make up-sim` currently **fails** by design. `sim-engine`
> is still a stub that exits with code 2, so `docker compose up --wait` reports an error.
> `tms-service` does come up (`curl localhost:8000/health`). This will be fixed once the
> engine loop exists.
