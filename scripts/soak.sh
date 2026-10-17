#!/usr/bin/env bash
# Soak test: run the simulator in live mode for SOAK_HOURS of wall time with a scripted crash
# (SIGKILL) and a Redpanda pause at random times; sample RSS, disk growth and spill backlog every
# SAMPLE_SECONDS into a CSV; finally stop with SIGTERM (must exit 0) and reconcile.
#
#   SOAK_MODE=host       (default; dev Mac) simulator runs on the host from .venv against the
#                        Compose core stack, in its own database vlsim_soak.
#   SOAK_MODE=container  (server) the real containerized stack (compose.yaml +
#                        compose.server.yaml) as an ISOLATED project "vlsimsoak" on its own
#                        named volumes and ports (+100), so memory limits, restart policy and
#                        health checks are exercised. The crash is a host-side SIGKILL, so the
#                        container's restart policy has to bring it back (docker kill would
#                        count as a manual stop and not restart).
#
# Bash on purpose: it reads the wall clock and sleeps, which Python code outside
# vlsim/engine/live.py may not do. Usage: make soak [SOAK_HOURS=24] [SOAK_SPEED=60] [SOAK_MODE=...]
set -euo pipefail

SOAK_MODE=${SOAK_MODE:-host}
SOAK_HOURS=${SOAK_HOURS:-24}
SOAK_SPEED=${SOAK_SPEED:-60}          # sim seconds per wall second (1 = real time)
SAMPLE_SECONDS=${SAMPLE_SECONDS:-300}
PAUSE_SECONDS=${PAUSE_SECONDS:-120}   # length of the Redpanda outage
PRESET=${PRESET:-s}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_ID=${RUN_ID:-soak-$STAMP}
OUT=${SOAK_DIR:-data/soak/$RUN_ID}
DOCKER=$(command -v docker || echo /Applications/Docker.app/Contents/Resources/bin/docker)
TOTAL=$(awk -v h="$SOAK_HOURS" 'BEGIN { printf "%d", h * 3600 }')
KILL_AT=$(( TOTAL * (10 + RANDOM % 36) / 100 ))    # between 10% and 45% of the run
PAUSE_AT=$(( TOTAL * (55 + RANDOM % 31) / 100 ))   # between 55% and 85% of the run
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
CSV=$OUT/samples.csv
LOG=$OUT/sim.log

note() { echo "[soak $(date -u +%H:%M:%S)] $*" | tee -a "$OUT/events.log"; }
fail() { note "FAIL: $*"; echo "FAIL: $*" > "$OUT/result.txt"; exit 1; }

if [ "$SOAK_MODE" = container ]; then
  # Isolated copy of the server stack: own project, named volumes, ports +100.
  ENVF=$OUT/soak.env
  { [ -f .env ] && grep -E '^(TMS_PG_PASSWORD|VLSIM_IMAGE_TAG)=' .env || true; } > "$ENVF"
  grep -q '^TMS_PG_PASSWORD=' "$ENVF" || echo "TMS_PG_PASSWORD=$(openssl rand -hex 24)" >> "$ENVF"
  grep -q '^VLSIM_IMAGE_TAG=' "$ENVF" || echo "VLSIM_IMAGE_TAG=soak" >> "$ENVF"
  cat >> "$ENVF" <<EOF
COMPOSE_PROJECT_NAME=vlsimsoak
COMPOSE_FILE=compose.yaml:compose.server.yaml
COMPOSE_PROFILES=core,sim
BIND_ADDR=127.0.0.1
TMS_PG_PORT=55532
REDPANDA_PORT=19192
TMS_API_PORT=8100
SIM_HTTP_PORT=8181
REDPANDA_ADVERTISE_HOST=127.0.0.1
VLSIM_PRESET=$PRESET
VLSIM_SPEED=$SOAK_SPEED
VLSIM_RUN_ID=$RUN_ID
EOF
  COMPOSE=("$DOCKER" compose --env-file "$ENVF")
  PORT=8181
  SIM_CONTAINER=vlsimsoak-sim-engine-1
else
  COMPOSE=("$DOCKER" compose --profile core)
  PORT=${SOAK_HTTP_PORT:-18081}
  DB=vlsim_soak
  VLSIM=.venv/bin/vlsim               # the real process, so kill -9 hits the simulator itself
  export VLSIM_TMS_DSN="postgresql+psycopg://vlsim:${TMS_PG_PASSWORD:-vlsim}@localhost:${TMS_PG_PORT:-55432}/$DB"
fi

PID="" ; PAUSED=0
cleanup() {
  if [ "$PAUSED" = 1 ]; then "${COMPOSE[@]}" unpause redpanda >/dev/null 2>&1 || true; fi
  if [ "$SOAK_MODE" = host ] && [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID"; wait "$PID" || true
  fi
}
trap cleanup EXIT

# --- mode-specific operations ---------------------------------------------------------------
start_sim() {
  if [ "$SOAK_MODE" = container ]; then
    "${COMPOSE[@]}" up -d --wait >> "$LOG" 2>&1 || fail "stack did not become healthy"
    PID=$("$DOCKER" inspect -f '{{.State.Pid}}' "$SIM_CONTAINER")
  else
    "$VLSIM" run --preset "$PRESET" --speed "$SOAK_SPEED" --run-id "$RUN_ID" --http-port "$PORT" \
      --state-dir "$OUT/state" --ground-truth-dir "$OUT/gt" >> "$LOG" 2>&1 &
    PID=$!
  fi
  note "simulator started pid=$PID"
}

alive() {
  if [ "$SOAK_MODE" = container ]; then
    [ "$("$DOCKER" inspect -f '{{.State.Running}}' "$SIM_CONTAINER" 2>/dev/null)" = true ]
  else
    kill -0 "$PID" 2>/dev/null
  fi
}

crash() {  # SIGKILL from outside, like an OOM kill or a segfault
  if [ "$SOAK_MODE" = container ]; then
    local before; before=$("$DOCKER" inspect -f '{{.RestartCount}}' "$SIM_CONTAINER")
    "$DOCKER" run --rm --pid=host alpine:3.20 kill -9 "$PID"
    note "SIGKILL pid=$PID (host side); waiting for the restart policy"
    for _ in $(seq 1 60); do
      sleep 5
      if [ "$("$DOCKER" inspect -f '{{.RestartCount}}' "$SIM_CONTAINER")" -gt "$before" ] && \
         [ "$("$DOCKER" inspect -f '{{.State.Health.Status}}' "$SIM_CONTAINER")" = healthy ]; then
        PID=$("$DOCKER" inspect -f '{{.State.Pid}}' "$SIM_CONTAINER"); note "restarted by policy pid=$PID"; return
      fi
    done
    fail "container was not restarted healthy by its restart policy within 5 minutes"
  else
    note "kill -9 pid=$PID"; kill -9 "$PID"; wait "$PID" || true; start_sim
  fi
}

graceful_stop() {
  if [ "$SOAK_MODE" = container ]; then
    "${COMPOSE[@]}" stop sim-engine >> "$LOG" 2>&1
    "$DOCKER" inspect -f '{{.State.ExitCode}}' "$SIM_CONTAINER"
  else
    kill -TERM "$PID"; set +e; wait "$PID"; local code=$?; set -e; PID=""; echo "$code"
  fi
}

reconcile_run() {
  if [ "$SOAK_MODE" = container ]; then
    "${COMPOSE[@]}" run --rm --no-deps sim-engine vlsim reconcile --run-id "$RUN_ID" \
      --ground-truth-dir /groundtruth > "$OUT/reconcile.json" 2>>"$LOG"
  else
    uv run vlsim reconcile --run-id "$RUN_ID" --ground-truth-dir "$OUT/gt" > "$OUT/reconcile.json" 2>>"$LOG"
  fi
}

metric() { curl -s --max-time 3 "localhost:$PORT/metrics" 2>/dev/null | awk -v m="$1" '$1 == m { print $2 }' || true; }

to_kb() {  # docker stats "123.4MiB / 480MiB" -> kB
  awk '{ v=$1; u=v; gsub(/[0-9.]/,"",u); gsub(/[A-Za-z]/,"",v);
         f = (u=="GiB") ? 1048576 : (u=="MiB") ? 1024 : (u=="KiB") ? 1 : 0.001; printf "%d", v*f }'
}

sample() {  # best effort: a probe that fails leaves an empty column, never aborts the soak
  local event=$1 now elapsed rss state_kb gt_kb rp_kb pg_bytes spill sim drift health restarts
  set +e
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ); elapsed=$(( $(date +%s) - T0 ))
  if [ "$SOAK_MODE" = container ]; then
    rss=$("$DOCKER" stats --no-stream --format '{{.MemUsage}}' "$SIM_CONTAINER" 2>/dev/null | to_kb)
    state_kb=$("${COMPOSE[@]}" exec -T sim-engine du -sk /state 2>/dev/null | cut -f1)
    gt_kb=$("${COMPOSE[@]}" exec -T sim-engine du -sk /groundtruth 2>/dev/null | cut -f1)
    restarts=$("$DOCKER" inspect -f '{{.RestartCount}}' "$SIM_CONTAINER" 2>/dev/null)
    pg_bytes=$("${COMPOSE[@]}" exec -T tms-postgres psql -U vlsim -d tms -Atc \
      "SELECT pg_database_size('tms')" 2>/dev/null)
  else
    rss=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ')
    state_kb=$(du -sk "$OUT/state" 2>/dev/null | cut -f1); gt_kb=$(du -sk "$OUT/gt" 2>/dev/null | cut -f1)
    restarts=""
    pg_bytes=$("${COMPOSE[@]}" exec -T tms-postgres psql -U vlsim -d tms -Atc \
      "SELECT pg_database_size('$DB')" 2>/dev/null)
  fi
  rp_kb=$("${COMPOSE[@]}" exec -T redpanda du -sk /var/lib/redpanda/data 2>/dev/null | cut -f1)
  spill=$(metric vlsim_spill_backlog_bytes); sim=$(metric vlsim_sim_time_seconds)
  drift=$(metric vlsim_pacing_drift_seconds)
  health=$(curl -s --max-time 5 "localhost:$PORT/health" | sed -E 's/.*"status": "([a-z]+)".*/\1/')
  echo "$now,$elapsed,$event,$PID,${rss:-},${state_kb:-},${gt_kb:-},${rp_kb:-},${pg_bytes:-},${spill:-},${sim:-},${drift:-},${health:-},${restarts:-}" >> "$CSV"
  set -e
}

# --- setup --------------------------------------------------------------------------------------
note "mode=$SOAK_MODE run=$RUN_ID hours=$SOAK_HOURS speed=${SOAK_SPEED}x preset=$PRESET crash@${KILL_AT}s pause@${PAUSE_AT}s (+${PAUSE_SECONDS}s) out=$OUT"
if [ "$SOAK_MODE" = container ]; then
  "${COMPOSE[@]}" build -q >> "$LOG" 2>&1
else
  "${COMPOSE[@]}" up -d --wait >/dev/null
  "${COMPOSE[@]}" exec -T tms-postgres psql -q -U vlsim -d tms \
    -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" -c "CREATE DATABASE $DB" >/dev/null
  uv sync -q --frozen
  uv run alembic upgrade head >/dev/null 2>&1
fi
echo "wall_utc,elapsed_s,event,pid,rss_kb,state_kb,groundtruth_kb,redpanda_kb,postgres_bytes,spill_backlog_bytes,sim_time_s,pacing_drift_s,health,restart_count" > "$CSV"

# --- run ----------------------------------------------------------------------------------------
T0=$(date +%s)
start_sim
NEXT_SAMPLE=0 ; CRASHED=0 ; PAUSE_DONE=0 ; UNPAUSE_AT=0
while :; do
  ELAPSED=$(( $(date +%s) - T0 ))
  [ "$ELAPSED" -ge "$TOTAL" ] && break
  alive || fail "simulator is not running (unexpected exit; see $LOG)"
  if [ "$CRASHED" = 0 ] && [ "$ELAPSED" -ge "$KILL_AT" ]; then
    sample crash_before; crash; CRASHED=1; sample after_restart
  fi
  if [ "$PAUSE_DONE" = 0 ] && [ "$PAUSED" = 0 ] && [ "$ELAPSED" -ge "$PAUSE_AT" ]; then
    "${COMPOSE[@]}" pause redpanda >/dev/null; PAUSED=1; UNPAUSE_AT=$(( ELAPSED + PAUSE_SECONDS ))
    note "redpanda paused"; sample redpanda_paused
  fi
  if [ "$PAUSED" = 1 ] && [ "$ELAPSED" -ge "$UNPAUSE_AT" ]; then
    sample redpanda_unpausing; "${COMPOSE[@]}" unpause redpanda >/dev/null; PAUSED=0; PAUSE_DONE=1
    note "redpanda unpaused"
  fi
  if [ "$ELAPSED" -ge "$NEXT_SAMPLE" ]; then sample periodic; NEXT_SAMPLE=$(( NEXT_SAMPLE + SAMPLE_SECONDS )); fi
  sleep 5
done

# --- stop, check, reconcile ---------------------------------------------------------------------
sample before_sigterm
note "SIGTERM (graceful stop)"
CODE=$(graceful_stop)
[ "$CODE" = 0 ] || fail "graceful stop exited with $CODE"
if [ "$SOAK_MODE" = container ]; then
  OOM=$("$DOCKER" inspect -f '{{.State.OOMKilled}}' "$SIM_CONTAINER")
  RESTARTS=$("$DOCKER" inspect -f '{{.RestartCount}}' "$SIM_CONTAINER")
  [ "$OOM" = false ] || fail "sim-engine was OOM-killed (480 MB limit)"
  [ "$RESTARTS" = 1 ] || fail "sim-engine restarted $RESTARTS times (expected exactly 1: our SIGKILL)"
fi
note "stopped cleanly (exit 0); reconciling"
reconcile_run || fail "reconcile failed (see $OUT/reconcile.json)"
MAX_RSS=$(awk -F, 'NR > 1 && $5 != "" { if ($5 > m) m = $5 } END { print m }' "$CSV")
note "PASS: ${SOAK_HOURS}h at ${SOAK_SPEED}x ($SOAK_MODE), crash + restart and ${PAUSE_SECONDS}s Redpanda pause survived; reconcile ok; max RSS ${MAX_RSS} kB"
echo "PASS" > "$OUT/result.txt"
if [ "$SOAK_MODE" = container ] && [ "${SOAK_KEEP:-0}" != 1 ]; then
  "${COMPOSE[@]}" down -v >> "$LOG" 2>&1 && note "isolated soak stack and its volumes removed (SOAK_KEEP=1 keeps them)"
fi
