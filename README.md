# VietLink Express simulator

A 24/7 discrete-event simulator that plays the source systems of a fictional Vietnamese
logistics company (TMS Postgres, Redpanda scan and GPS topics) for a separate data platform.
See [docs/SPEC.md](docs/SPEC.md). Full run instructions and an architecture diagram arrive in M6.

## Quick start (development)

```bash
make up      # Postgres + Redpanda (Compose profile "core")
make lint
make test
make down
```
