# ADR 0013: Server deployment layout (x86 Ubuntu 24.04, 16 GB)

**Status:** accepted (2026-10-17)

**Decision.**

*Compose files* (selected through `COMPOSE_FILE` in `.env`)
- `compose.yaml`: shared base.
- `compose.server.yaml`: production Redpanda flags, required secrets, image tag.
- `compose.server-data.yaml`: bind mounts under `DATA_ROOT`.

Keeping data placement separate lets the soak run the exact server stack as an isolated project on named volumes.

*Configuration*
- Everything comes from `.env`, copied from `deploy/server.env.example` with `chmod 600`. The file is gitignored.
- The Postgres password is required on the server (`${TMS_PG_PASSWORD:?}`); the dev default `vlsim` applies only without the server overlay.
- Images are built on the server and tagged with the git short SHA (`VLSIM_IMAGE_TAG`). Rollback reuses the previous local image.

*Resilience*
- `restart: unless-stopped` on every container. A crash or OOM kill restarts sim-engine, which resumes from its checkpoint (ADRs 0008, 0011, 0012).
- `docker kill` and `docker stop` count as manual stops, so the policy does not apply to them. The soak therefore crashes the container with a host-side SIGKILL.
- `stop_grace_period: 2m` lets a SIGTERM finish the final checkpoint.

*Memory* (`mem_limit` = `memswap_limit`, so no swap)

| Container | Limit |
|---|---|
| postgres | 2 GiB |
| redpanda | 1.5 GiB (Seastar `--memory=1G`) |
| tms-service | 256 MiB |
| sim-engine | 480 MiB |

Measured sim-engine peak RSS: 130 MB at S and 180 MB at M. Unacknowledged producer messages are bounded by `max_inflight` (200k) and librdkafka's queue cap, so an outage spills to disk rather than growing memory.

*Logs*
- Docker's json-file driver with 5 × 20 MB per container.

*Ports*
- Every published port binds `${BIND_ADDR:-127.0.0.1}`. Remote access goes through `ssh -L` or `tailscale serve`, or `BIND_ADDR` can be set to the Tailscale IP (boot-order caveat in the README).
- Docker-published ports bypass `ufw`, so `0.0.0.0` is never used.

*Redpanda on the server*
- Not `dev-container`: `developer_mode=false`, `write_caching_default=false` (acknowledged writes are fsynced), `auto_create_topics_enabled=false`, `log_retention_ms=7d`, `storage_min_free_bytes=5 GiB`, `--overprovisioned` (shared host), `--smp=1`, `--memory=1G`.
- The image's default `redpanda.yaml` enables developer mode and write caching. Both overrides were verified on a running broker (`developer_mode:0` in its log; `write_caching_default=false`). rpk still prints a "development purposes" banner because of `--overprovisioned`.
- Per-topic partitions and retention caps are applied by sim-engine on every start (ADR 0010).

**Verified on the dev Mac** (separate project and ports, named volumes):
- All four containers healthy with the limits applied; sim-engine at 88 MiB of 480 MiB.
- Topic configs correct.
- A host-side SIGKILL restarted sim-engine through its policy, and it resumed from the checkpoint.
- `docker compose stop` exited 0, and reconcile inside the container passed.

**Not verified here:**
- Bind-mount ownership on Linux. Docker Desktop cannot chown host directories; Postgres initdb fails on a Mac bind mount, which is expected.
- Native x86 performance.
- Tailscale binding.
