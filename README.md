# VietLink Express simulator

A 24/7 discrete-event simulator that plays the source systems of a fictional Vietnamese logistics company for a separate data platform:
- **TMS Postgres** (OLTP rows that are updated in place, ready for CDC);
- **Redpanda** topics `tracking.scans` and `telemetry.gps`;
- a sealed **ground-truth** log of everything that was emitted.

The specification is [docs/SPEC.md](docs/SPEC.md). Design decisions are in [docs/adr/](docs/adr/), modelling assumptions in [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md), measurements in [docs/BENCHMARKS.md](docs/BENCHMARKS.md), and the shipment lifecycle in [docs/STATE_MACHINE.md](docs/STATE_MACHINE.md).

## Architecture

```mermaid
flowchart LR
    subgraph sim["sim-engine (vlsim run)"]
        clock["Sim clock + event queue<br/>seeded RNG streams"] --> handlers["Business handlers<br/>demand · docks · line-haul · last mile"]
        handlers --> outbox["Outbox<br/>scans + vectorized GPS batches"]
        handlers --> tmsapi["TmsService (in-process)<br/>write-behind, monotonic upserts"]
        pacer["Live pacer 1x–1000x<br/>(engine/live.py: only wall-clock reader)"] -.-> clock
        ckpt[("SQLite checkpoints<br/>/state")]
        clock <--> ckpt
    end
    tmsapi -- "1. commit" --> pg[("tms-postgres<br/>wal_level=logical")]
    outbox -- "2. after commit" --> gt[("Ground truth JSONL/gz<br/>/groundtruth (sealed)")]
    gt --> inj["Fault injector<br/>(pass-through in MVP)"]
    inj --> prod["Producer<br/>disk spill + backpressure"]
    prod --> rp[("Redpanda<br/>tracking.scans · telemetry.gps")]
    tmssvc["tms-service<br/>FastAPI /health"] --> pg
    prom["Prometheus / curl"] -. "/metrics /health /speed :8081" .-> sim
    pg -. "CDC (platform side, later)" .-> platform["Data platform<br/>(separate repo)"]
    rp -.-> platform
```

Order within every flush (ADR 0009): **Postgres commits first**, then the matching events go to ground truth, then the fault injector, then Redpanda. Topics never run ahead of the database.

## Quick start

```bash
make up                 # Postgres + Redpanda (Compose profile "core"), waits until healthy
make migrate            # TMS schema (Alembic)
make seed               # deterministic master data (PRESET=s|m)
make topics             # tracking.scans + telemetry.gps: 6 partitions, 7-day retention
uv run vlsim simulate --days 7 --run-id demo   # fast batch run (no pacing)
make reconcile RUN=demo                         # ground truth vs topics vs Postgres
```

Live mode, as a container:

```bash
VLSIM_SPEED=60 make up-sim          # tms-service + sim-engine on top of core
curl localhost:8081/health          # {"status": "ok" | "degraded" | "failing", ...}
curl -X POST "localhost:8081/speed?x=500"   # change speed at runtime (1–1000)
docker compose --profile core --profile sim stop sim-engine   # SIGTERM: final checkpoint, exit 0
```

Live mode, from the host:

```bash
uv run vlsim run --speed 1000 --run-id live --state-dir data/state --ground-truth-dir data/groundtruth
```

## Make targets

| Target | What it does |
|---|---|
| `make up` / `make down` | Start or stop the core services (Postgres, Redpanda) |
| `make up-sim` | Core + `tms-service` + `sim-engine` (live, `VLSIM_SPEED`, default 1) |
| `make clean` | Down **and delete volumes** (TMS data, topics, ground truth, checkpoints) |
| `make migrate` / `make migrate-down` | Apply or roll back the TMS schema |
| `make seed PRESET=s` | Idempotent master data |
| `make topics` | Create topics and align their retention |
| `make lint` / `make test` / `make test-integration` | ruff + mypy / unit tests / tests against Postgres + Redpanda (needs `make up`) |
| `make reconcile RUN=<id>` | Compare a run's ground truth with Redpanda and Postgres; exits 1 on a mismatch |
| `make soak [SOAK_HOURS=24] [SOAK_SPEED=60] [SOAK_MODE=host\|container]` | Live soak with a scripted crash and a Redpanda pause; CSV samples; then reconcile |
| `make clean-scratch` | Drop scratch databases (`vlsim_*`); never touches `tms` |

## Soak test (24 h)

`make soak` runs scale S live for `SOAK_HOURS` (default 24) at `SOAK_SPEED` (default 60x, so about 60 sim days; use 1 for real time). At random times it kills the simulator with `kill -9` and restarts it, and pauses Redpanda for `PAUSE_SECONDS` (120). Every `SAMPLE_SECONDS` (300) it samples RSS, disk (state, ground truth, Redpanda, Postgres), spill backlog, pacing drift and health into `data/soak/<run>/samples.csv`. At the end it stops the simulator with SIGTERM (it must exit 0) and runs reconcile. The result is in `data/soak/<run>/result.txt`.

```bash
caffeinate -dimsu &        # macOS: keep the machine awake for the whole run
make up
make soak                  # 24 h at 60x; or: make soak SOAK_HOURS=24 SOAK_SPEED=1
cat data/soak/*/result.txt
```

It uses its own database (`vlsim_soak`), its own run id and port 18081, so it does not touch `tms` or the Compose sim-engine. A short rehearsal: `make soak SOAK_HOURS=0.1`.

## Deploy (x86 Ubuntu Server 24.04, 16 GB)

The server runs the same images as dev with three Compose files:

| File | Role |
|---|---|
| `compose.yaml` | Base: restart policies, memory limits, log rotation (5 × 20 MB per container), ports bound to `${BIND_ADDR:-127.0.0.1}` |
| `compose.server.yaml` | Redpanda without dev mode (fsync on, write caching off, 7-day retention, 5 GiB min free), required secrets, image tag |
| `compose.server-data.yaml` | The four volumes bound to directories under `DATA_ROOT` on the big disk |

All settings live in `.env` (from `deploy/server.env.example`, `chmod 600`, never committed).

Memory limits (no swap):

| Container | Limit |
|---|---|
| tms-postgres | 2 GiB |
| redpanda | 1.5 GiB (Seastar `--memory=1G`) |
| tms-service | 256 MiB |
| sim-engine | **480 MiB** (measured peak RSS: 130 MB at S, 180 MB at M) |

Total: about 4.2 GiB of 16 GiB. Topic partitions, retention and size caps (ADR 0010) are applied by sim-engine on every start.

### First-time setup

Prerequisites on Ubuntu Server 24.04:

| Prerequisite | Why |
|---|---|
| **Docker Engine** with the **compose plugin** (`docker compose`, not the old `docker-compose`) | Runs the stack |
| **git** | Clone, upgrade and roll back |
| **uv** 0.12.13 | Host-side tools: reconcile, tests |
| **Python 3.12** | Ubuntu 24.04 ships it as `python3`; uv uses it, or downloads 3.12 itself, because the project pins `.python-version` 3.12 |
| **tmux** | Keeps a soak running when SSH drops |
| make, curl, openssl, ca-certificates | Used by the Makefile, the soak script and the setup commands |

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git make openssl tmux python3 python3-venv
```
```bash
sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
```
```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
```
```bash
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```
```bash
sudo usermod -aG docker "$USER"   # then log out and back in
```
```bash
curl -LsSf https://astral.sh/uv/0.12.13/install.sh | sh   # host-side tools: reconcile, tests, host-mode soak
```

Open a new login shell (for the docker group and uv on PATH), then check every prerequisite:

```bash
docker --version && docker compose version && git --version && uv --version && python3 --version && tmux -V && make --version | head -1
```

Expected: Docker Engine 27 or newer, Docker Compose v2.x, git, `uv 0.12.13`, `Python 3.12.x`, tmux, GNU Make. `docker run --rm hello-world` must work without sudo.

Create the data directories on the 2 TB disk. `/srv/vlsim` must be on that disk: mount it there, or set `DATA_ROOT` to wherever it is mounted.

```bash
sudo mkdir -p /srv/vlsim/postgres /srv/vlsim/redpanda /srv/vlsim/groundtruth /srv/vlsim/state
```
Set each directory's owner to the UID its container runs as:

| Directory | Container | UID:GID | Image user |
|---|---|---|---|
| `/srv/vlsim/postgres` | tms-postgres | **999:999** | `postgres` in `postgres:17.11-bookworm` |
| `/srv/vlsim/redpanda` | redpanda | **101:101** | `redpanda` in `redpandadata/redpanda:v26.1.18` |
| `/srv/vlsim/groundtruth` | sim-engine | **10001:10001** | `vlsim` in our image |
| `/srv/vlsim/state` | sim-engine | **10001:10001** | `vlsim` in our image |

```bash
sudo chown 999:999 /srv/vlsim/postgres && sudo chmod 700 /srv/vlsim/postgres
```
```bash
sudo chown 101:101 /srv/vlsim/redpanda
```
```bash
sudo chown 10001:10001 /srv/vlsim/groundtruth /srv/vlsim/state && sudo chmod 700 /srv/vlsim/groundtruth
```
```bash
ls -ln /srv/vlsim
```

The last command must show owners 999, 101, 10001 and 10001. To confirm the UIDs from the pinned images:

```bash
docker run --rm --entrypoint id postgres:17.11-bookworm postgres && docker run --rm --entrypoint id redpandadata/redpanda:v26.1.18
```

Expected output: `uid=999(postgres)` and `uid=101(redpanda)`. Our image's UID 10001 is set in `docker/Dockerfile`. The Postgres image would also chown its directory itself on first start, but setting it explicitly avoids a failed first start if the directory already holds files.

Clone, configure and start:

```bash
git clone https://github.com/fvncent/logistics-sim.git ~/logistics-sim && cd ~/logistics-sim
```
```bash
cp deploy/server.env.example .env && chmod 600 .env
```
```bash
PW=$(openssl rand -hex 24) && sed -i "s/^TMS_PG_PASSWORD=.*/TMS_PG_PASSWORD=$PW/; s#vlsim:CHANGE_ME@#vlsim:$PW@#; s/^VLSIM_IMAGE_TAG=.*/VLSIM_IMAGE_TAG=$(git rev-parse --short HEAD)/" .env && unset PW
```
```bash
docker compose config -q && docker compose build && docker compose up -d --wait
```
```bash
docker compose ps && curl -s localhost:8081/health && curl -s localhost:8081/metrics | grep -E '^vlsim_(sim_time|speed|spill)'
```

`tms-service` applies the migrations, `sim-engine` seeds master data (idempotent) and creates the topics. Live mode then runs at `VLSIM_SPEED` (`.env`; default 1x, M preset). `docker compose stop sim-engine` stops it gracefully (final checkpoint, exit 0). `restart: unless-stopped` brings everything back after a crash or a reboot. Check the reproducibility golden once on the server: `uv run pytest tests/unit/test_reproducibility.py` (see "Differences from the dev Mac").

Reaching it from your laptop:
- **Recommended:** keep `BIND_ADDR=127.0.0.1` and use `ssh -L 8081:127.0.0.1:8081 server`, or `tailscale serve --bg --tcp 8081 tcp://127.0.0.1:8081`.
- **Binding the Tailscale interface directly:** set `BIND_ADDR` to the server's `100.x.y.z`. The interface must exist before Docker starts containers at boot. Add `printf '[Unit]\nAfter=tailscaled.service\nWants=tailscaled.service\n' | sudo tee /etc/systemd/system/docker.service.d/10-after-tailscale.conf && sudo systemctl daemon-reload`. This is **untested here**.
- For Kafka clients on other machines, set `REDPANDA_ADVERTISE_HOST` to an address they can reach. The default is `127.0.0.1`, deliberately not `localhost`, which Ubuntu resolves to IPv6 `::1` first while ports are published on IPv4 only (ADR 0004).
- Never use `0.0.0.0`: Docker-published ports bypass `ufw`.

### Upgrade

```bash
cd ~/logistics-sim && git fetch --tags && OLD=$(git rev-parse --short HEAD)
```
```bash
docker compose stop sim-engine   # graceful: final checkpoint
```
```bash
mkdir -p ~/backups && docker compose exec -T tms-postgres pg_dump -U vlsim -Fc tms > ~/backups/tms-$OLD.dump && sudo tar -C /srv/vlsim -czf ~/backups/state-$OLD.tgz state
```
```bash
git checkout <new-tag-or-commit> && git diff --stat "$OLD"..HEAD -- config ':!config/transport.yaml' migrations
```
```bash
sed -i "s/^VLSIM_IMAGE_TAG=.*/VLSIM_IMAGE_TAG=$(git rev-parse --short HEAD)/" .env && docker compose build && docker compose up -d --wait
```

- **If the diff lists nothing:** the new image resumes the same run from its checkpoint.
- **If `config/` changed (other than `transport.yaml`):** the config fingerprint changed and sim-engine will refuse the old checkpoint (`config fingerprint differs`, crash loop). Treat it as a **new run**:
  1. `docker compose stop sim-engine`
  2. Archive and empty `/srv/vlsim/state` and `/srv/vlsim/groundtruth/<old run id>`.
  3. Recreate the `tms` database: `docker compose exec -T tms-postgres psql -U vlsim -d postgres -c 'DROP DATABASE tms WITH (FORCE)' -c 'CREATE DATABASE tms'`, then `docker compose restart tms-service`.
  4. Set a new `VLSIM_RUN_ID` in `.env`.
  5. `docker compose up -d --wait`
- **If `migrations/` changed:** `tms-service` applies them on start.

### Rollback

```bash
docker compose stop sim-engine
```
```bash
docker compose exec tms-service alembic downgrade <previous-revision>   # only if the upgrade added a migration
```
```bash
git checkout "$OLD" && sed -i "s/^VLSIM_IMAGE_TAG=.*/VLSIM_IMAGE_TAG=$OLD/" .env && docker compose up -d --wait
```

The old image is still present locally, so no rebuild is needed. If the upgrade started a new run (config change), the checkpoints were written by the newer config. In that case restore the backups instead:
1. `sudo tar -C /srv/vlsim -xzf ~/backups/state-$OLD.tgz`
2. `docker compose exec -T tms-postgres pg_restore -U vlsim -d tms --clean < ~/backups/tms-$OLD.dump` (pg_restore reads the dump from stdin)
3. Set `VLSIM_RUN_ID` back.

### Soaks on the server

#### Before a soak: keep the host from rebooting or restarting Docker

`unattended-upgrades` can reboot the machine (`Automatic-Reboot`), and `needrestart` can restart services after library updates, including `dockerd`, which restarts every container. Either one ends a soak. Before a soak:

```bash
apt-config dump | grep -i 'Unattended-Upgrade::Automatic-Reboot'
```

If that prints `"true"`, or to make sure regardless:

```bash
echo 'Unattended-Upgrade::Automatic-Reboot "false";' | sudo tee /etc/apt/apt.conf.d/99vlsim-no-auto-reboot
```

For the duration of the soak, also stop the automatic apt runs entirely and pin Docker:

```bash
sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service
```
```bash
sudo apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin
```
```bash
[ -f /var/run/reboot-required ] && echo "REBOOT PENDING: reboot now, before starting the soak" || echo "no reboot pending"
```

After the soaks, re-enable everything and install whatever was held back:

```bash
sudo systemctl start apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service && sudo apt-mark unhold docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

`99vlsim-no-auto-reboot` can stay, so reboots remain manual. Delete it if you want automatic reboots back.

#### 1-hour smoke first

Run this before either 24-hour soak. It exercises the same crash, pause, SIGTERM and reconcile sequence in one hour (60 sim hours at 60x):

```bash
cd ~/logistics-sim && make soak SOAK_MODE=container SOAK_HOURS=1 SOAK_SPEED=60 PRESET=s
```

Only start the 24-hour soaks if `result.txt` says `PASS`.

#### 24-hour soaks

Run them inside `tmux` so an SSH disconnect does not end them. `SOAK_MODE=container` runs the **real server stack** (compose.yaml + compose.server.yaml) as an isolated project `vlsimsoak`. It uses its own named volumes and ports (55532, 19192, 8100, 8181), so it does not touch the deployed stack, though both share the machine's RAM and CPU. The crash is a host-side SIGKILL, so the container's restart policy must bring it back. The run also fails if sim-engine was OOM-killed (480 MiB limit) or restarted more than once. The two soaks share the project name, so run them one after the other.

```bash
tmux new -s soak
```
```bash
cd ~/logistics-sim && make soak SOAK_MODE=container SOAK_HOURS=24 SOAK_SPEED=60 PRESET=s
```
```bash
cd ~/logistics-sim && make soak SOAK_MODE=container SOAK_HOURS=24 SOAK_SPEED=1 PRESET=s
```

The first is 24 h accelerated (about 60 sim days, including the run-up to 11.11). The second is a real-time soak (24 h = 1 sim day). Add `PRESET=m` to soak the default server preset.

Reading the results (`data/soak/<run>/`):

```bash
cat data/soak/*/result.txt && cat data/soak/*/events.log
```
```bash
column -s, -t < "$(ls -d data/soak/*/ | tail -1)samples.csv" | less -S
```
```bash
awk -F, 'NR>1 && $5!="" {if($5>m)m=$5} END{print "max RSS kB", m}' "$(ls -d data/soak/*/ | tail -1)samples.csv"
```
```bash
python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print('ok', r['ok']); [print(t, {k: v[k] for k in ('ground_truth_events','topic_distinct','duplicates_in_topic','missing_from_topic')}) for t, v in r['topics'].items()]; print('postgres', r['postgres']['status_mismatches'], 'mismatches')" "$(ls -d data/soak/*/ | tail -1)reconcile.json"
```

`samples.csv` columns (one row every 5 min, plus rows at each event):

| Column | Meaning |
|---|---|
| `wall_utc`, `elapsed_s` | When the sample was taken |
| `event` | periodic / crash_before / after_restart / redpanda_paused / redpanda_unpausing / before_sigterm |
| `pid` | Simulator PID (changes after the crash) |
| `rss_kb` | Simulator memory. It should plateau; a steady climb over hours is a leak |
| `state_kb` | Checkpoints and spill |
| `groundtruth_kb` | Grows about 7.5 MB per sim day at S (gzipped) |
| `redpanda_kb` | Levels off once the 7-day retention or the size caps kick in |
| `postgres_bytes` | TMS database size |
| `spill_backlog_bytes` | > 0 only during and just after the Redpanda pause, then back to 0 |
| `sim_time_s`, `pacing_drift_s` | Sim clock and pacing drift. Drift spikes during the pause (checkpoints wait for acks) and should return to about 0 |
| `health` | ok / degraded (during the pause) / failing |
| `restart_count` | Container mode only; 1 after the crash |

`reconcile.json`:
- `ok` must be `true`.
- On a FAIL the isolated stack is left running for inspection. Remove it afterwards with `docker compose --env-file data/soak/<run>/soak.env down -v`.
- Per topic, `missing_from_topic` and `unexpected_in_topic` must be 0. `duplicates_in_topic` > 0 is expected after the crash.
- `postgres.status_mismatches` must be 0.

#### If `missing_from_topic` is not 0: retention eviction or real loss?

Reconcile reads each partition from the offset recorded when the run started (`run.json`). If Redpanda has since deleted the oldest segments, those events are counted as missing even though nothing was lost in delivery.

**What can evict during a soak:**
- **Time retention (7 days) cannot.** Messages carry the producer's **wall-clock** `CreateTime`, not sim time, so a 24 h soak, even at 60x, never reaches 7 days.
- **Only the per-partition size caps can.** They are `retention.bytes` = 1 GiB (GPS) and 256 MiB (scans) per partition (ADR 0010). Redpanda deletes whole closed 128 MiB segments, oldest first.

**Expected volume.** Measured on disk on the dev Mac (lz4, as stored): about **94 B per GPS message** and about **327 B per scan**, since scans go in small batches that compress poorly. GPS is keyed by vehicle; partition skew is 20% (S) and 17.5% (M) of vehicles in the busiest partition, which I widen to 30% for uneven activity. Scans are keyed by shipment, so they spread evenly.

| Soak | Sim days | GPS total | Busiest GPS partition | vs 1 GiB cap | Scans per partition | vs 256 MiB cap |
|---|---|---|---|---|---|---|
| S, 24 h at 60x | 60 | ≈ 8.4 M msgs ≈ 0.8 GB | ≈ 240 MB | 23% | ≈ 30 MB | 12% |
| M, 24 h at 60x | 60 | ≈ 24.6 M msgs ≈ 2.3 GB | ≈ 700 MB | **68%** | ≈ 140 MB | 55% |
| S or M, 24 h at 1x | 1 | negligible | negligible | under 1% | negligible | under 1% |

The 60 sim days include the 11.11 peak, which adds roughly 10–15%. **No eviction is expected in any of these soaks.** M at 60x is the closest, but still about 30% below the cap. A non-zero `missing_from_topic` is therefore presumed to be a real loss until the check below proves otherwise.

**The check.** Leave the failed stack up. Then compare, per partition, the run's recorded start offset with Redpanda's current `LOG-START-OFFSET`:

```bash
RUN=$(ls -d data/soak/*/ | tail -1); ID=$(basename "$RUN"); ENVF="${RUN}soak.env"
```
```bash
docker compose --env-file "$ENVF" run --rm --no-deps sim-engine cat "/groundtruth/$ID/run.json"
```
```bash
docker compose --env-file "$ENVF" exec redpanda rpk topic describe telemetry.gps -p && docker compose --env-file "$ENVF" exec redpanda rpk topic describe tracking.scans -p
```
```bash
docker compose --env-file "$ENVF" exec redpanda sh -c 'du -sk /var/lib/redpanda/data/kafka/telemetry.gps/* /var/lib/redpanda/data/kafka/tracking.scans/*'
```

| What you see | Verdict |
|---|---|
| For every partition, `LOG-START-OFFSET` ≤ the `start_offsets` value in `run.json` | **Nothing was evicted, so the missing events are a REAL LOSS.** Investigate: `events.log` (timing around the crash or pause), `sim.log`, and the emitter stats in the `live_stopped` line. |
| Some partitions have `LOG-START-OFFSET` > their start offset, that partition's `du` is near its cap (about 1,048,576 kB GPS / 262,144 kB scans), and `missing_from_topic` ≤ the sum of (`LOG-START-OFFSET` − start offset) over those partitions | **Retention eviction.** Those were the run's oldest events. Not a delivery bug, but the soak outgrew the caps: lower `SOAK_SPEED`, or raise the cap in `config/transport.yaml` (ADR 0010) |
| Eviction happened, but `missing_from_topic` is larger than the evicted count | **Both.** The excess over the evicted count is a real loss |

In host mode (`SOAK_MODE=host`), `run.json` is at `data/soak/<run>/gt/<run>/run.json` and the broker is the dev one: `docker compose --profile core exec redpanda rpk topic describe <topic> -p`.

### Differences from the dev Mac (Apple Silicon)

| Area | Dev Mac | Server | What to do |
|---|---|---|---|
| CPU / reproducibility | Darwin-arm64 golden | Linux-x86_64 golden (recorded under QEMU; real x86 GitHub runners matched on the M6 PR) | Run the reproducibility test once on the Ryzen. A mismatch fails loudly; record it deliberately with `--write-golden` only if you accept that platform's output |
| Redpanda durability | `--mode dev-container`: no fsync, write caching | developer_mode off, write caching off, fsync | The server is slower per write but durable; the benchmarks were measured on the Mac with dev-mode Redpanda |
| Bind-mount ownership | Docker Desktop hides UIDs (and Postgres bind mounts fail there) | Real UIDs | `chown` 101 / 10001 as above (verified only for named volumes here) |
| Memory limits | Inside Docker Desktop's VM | Native cgroups, no swap (`memswap_limit`) | An OOM kill restarts sim-engine and it resumes; the soak fails if it happens |
| Docker CLI | `~/.docker/bin`, Makefile fallback | `/usr/bin/docker` | none |
| Port 5432 | Taken by Homebrew Postgres | Free | Still 55432 for consistency |
| Keep awake | `caffeinate` | none | Use `tmux` |
| Throughput numbers | Measured on the M-series laptop | Not measured on the Ryzen 5 5600 | Re-run `vlsim simulate --days 7` on the server before relying on headroom figures |
| Tailscale | n/a | Not tested | See the caveat above |

## Ports

All ports are published on `${BIND_ADDR:-127.0.0.1}` only.

| Port (host) | Service | Notes |
|---|---|---|
| 55432 | tms-postgres | `TMS_PG_PORT`. Not 5432, because a native Postgres on the dev Mac holds it (ADR 0004) |
| 19092 | Redpanda Kafka API | `REDPANDA_PORT`. Inside the network: `redpanda:9092` |
| 8000 | tms-service | `GET /health` |
| 8081 | sim-engine | `GET /metrics` (Prometheus), `GET /health`, `GET\|POST /speed?x=N` |

## Volumes

| Volume | Mounted in | Contents |
|---|---|---|
| `tms-pgdata` | tms-postgres | TMS database (logical WAL for CDC, ADR 0006) |
| `redpanda-data` | redpanda | Topic data (7-day / size-capped retention, ADR 0010) |
| `groundtruth` | **sim-engine only** | `<run_id>/<topic>/<sim-date>.jsonl[.gz]`: the answer key; the platform must never read it |
| `sim-state` | sim-engine | `checkpoints.sqlite` and the producer's `spill.jsonl` |

## Determinism

- The same seed and config give byte-identical output **on the same OS and architecture**. CI checks golden hashes per OS-arch (`tests/golden/repro_hashes.json`).
- **Hashes may differ between arm64 and amd64**, or between macOS and Linux. Transcendental maths (numpy and libm `sin`, `cos`, `exp`, `log`) may round differently per platform and SIMD path, and a last-bit difference can shift a rounded coordinate or a millisecond.
- On 2026-10-17 all three platforms we checked agreed (Darwin-arm64, Linux-aarch64, Linux-x86_64), but that is not guaranteed (ADR 0012).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: command not found` (macOS) | Add `~/.docker/bin` to PATH (Docker Desktop does this in `~/.zprofile`; open a new login shell) |
| Tests or CLI connect to the wrong Postgres | A native Postgres holds 5432. The TMS runs on **55432**; check `TMS_PG_PORT` / `VLSIM_TMS_DSN` |
| `sqlite3.OperationalError: unable to open database file` in sim-engine | The `sim-state` / `groundtruth` volumes were created by an older image as root. If empty, `docker volume rm vlsim_sim-state vlsim_groundtruth`, then `make up-sim` |
| `/health` says `degraded` | Redpanda is unreachable or the producer is spilling to disk. The simulation continues; the backlog drains when Redpanda is back (`vlsim_spill_backlog_bytes`) |
| `/health` says `failing` (503) | Postgres is unreachable. The simulator retries with backoff and emits nothing until commits succeed |
| `config fingerprint differs from the checkpoint's` | Config changed under an existing run. Use a new `--state-dir`/`--run-id`, or `make clean` |
| `run X already holds a run` | Fresh start into a used ground-truth dir. Pick a new `--run-id` or resume with the same `--state-dir` |
| Reconcile reports missing events mid-run | Expected: topics trail Postgres by up to one flush. Reconcile after a clean stop (SIGTERM) |
| Ground truth disappeared on resume | By design: everything after the last checkpoint is truncated and rewritten identically by replay (ADR 0011) |
| Seed data changed after a config change (e.g. fleet mix) | `make clean && make up && make migrate && make seed` |
