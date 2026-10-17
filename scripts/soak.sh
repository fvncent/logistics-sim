#!/usr/bin/env bash
# Soak test: run scale S in live mode for SOAK_HOURS of wall time, with a scripted kill -9 and
# restart and a Redpanda pause at random times. RSS, disk growth and spill backlog are sampled
# every SAMPLE_SECONDS into a CSV. At the end: SIGTERM (expect exit 0), then reconcile.
#
# Bash on purpose: it reads the wall clock and sleeps, which Python code outside
# vlsim/engine/live.py may not do. Usage: make soak [SOAK_HOURS=24] [SOAK_SPEED=60]
set -euo pipefail

SOAK_HOURS=${SOAK_HOURS:-24}
SOAK_SPEED=${SOAK_SPEED:-60}          # sim seconds per wall second (1 = real time)
SAMPLE_SECONDS=${SAMPLE_SECONDS:-300}
PAUSE_SECONDS=${PAUSE_SECONDS:-120}   # length of the Redpanda outage
PRESET=${PRESET:-s}
PORT=${SOAK_HTTP_PORT:-18081}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_ID=${RUN_ID:-soak-$STAMP}
OUT=${SOAK_DIR:-data/soak/$RUN_ID}
DB=vlsim_soak
DOCKER=$(command -v docker || echo /Applications/Docker.app/Contents/Resources/bin/docker)
COMPOSE=("$DOCKER" compose --profile core)
VLSIM=.venv/bin/vlsim                 # the real process, so kill -9 hits the simulator itself

TOTAL=$(awk -v h="$SOAK_HOURS" 'BEGIN { printf "%d", h * 3600 }')
KILL_AT=$(( TOTAL * (10 + RANDOM % 36) / 100 ))    # between 10% and 45% of the run
PAUSE_AT=$(( TOTAL * (55 + RANDOM % 31) / 100 ))   # between 55% and 85% of the run
mkdir -p "$OUT"
CSV=$OUT/samples.csv
LOG=$OUT/sim.log
export VLSIM_TMS_DSN="postgresql+psycopg://vlsim:vlsim@localhost:${TMS_PG_PORT:-55432}/$DB"

note() { echo "[soak $(date -u +%H:%M:%S)] $*" | tee -a "$OUT/events.log"; }
fail() { note "FAIL: $*"; echo "FAIL: $*" > "$OUT/result.txt"; exit 1; }

PID="" ; PAUSED=0
cleanup() {
  if [ "$PAUSED" = 1 ]; then "${COMPOSE[@]}" unpause redpanda >/dev/null 2>&1 || true; fi
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then kill -TERM "$PID"; wait "$PID" || true; fi
}
trap cleanup EXIT

start_sim() {
  "$VLSIM" run --preset "$PRESET" --speed "$SOAK_SPEED" --run-id "$RUN_ID" --http-port "$PORT" \
    --state-dir "$OUT/state" --ground-truth-dir "$OUT/gt" >> "$LOG" 2>&1 &
  PID=$!
  note "simulator started pid=$PID"
}

metric() { curl -s --max-time 3 "localhost:$PORT/metrics" 2>/dev/null | awk -v m="$1" '$1 == m { print $2 }' || true; }

sample() {  # best effort: a probe that fails leaves an empty column, never aborts the soak
  local event=$1 now elapsed rss state_kb gt_kb rp_kb pg_bytes spill sim drift health
  set +e
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ); elapsed=$(( $(date +%s) - T0 ))
  rss=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ' || true)
  state_kb=$(du -sk "$OUT/state" 2>/dev/null | cut -f1); gt_kb=$(du -sk "$OUT/gt" 2>/dev/null | cut -f1)
  rp_kb=$("${COMPOSE[@]}" exec -T redpanda du -sk /var/lib/redpanda/data 2>/dev/null | cut -f1 || true)
  pg_bytes=$("${COMPOSE[@]}" exec -T tms-postgres psql -U vlsim -d tms -Atc \
    "SELECT pg_database_size('$DB')" 2>/dev/null || true)
  spill=$(metric vlsim_spill_backlog_bytes); sim=$(metric vlsim_sim_time_seconds)
  drift=$(metric vlsim_pacing_drift_seconds)
  health=$(curl -s --max-time 5 "localhost:$PORT/health" | sed -E 's/.*"status": "([a-z]+)".*/\1/' || true)
  echo "$now,$elapsed,$event,$PID,${rss:-},${state_kb:-},${gt_kb:-},${rp_kb:-},${pg_bytes:-},${spill:-},${sim:-},${drift:-},${health:-}" >> "$CSV"
  set -e
}

note "run=$RUN_ID hours=$SOAK_HOURS speed=${SOAK_SPEED}x preset=$PRESET kill@${KILL_AT}s pause@${PAUSE_AT}s (+${PAUSE_SECONDS}s) out=$OUT"
"${COMPOSE[@]}" up -d --wait >/dev/null
"${COMPOSE[@]}" exec -T tms-postgres psql -q -U vlsim -d tms \
  -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" -c "CREATE DATABASE $DB" >/dev/null
uv sync -q --frozen
uv run alembic upgrade head >/dev/null 2>&1
echo "wall_utc,elapsed_s,event,pid,rss_kb,state_kb,groundtruth_kb,redpanda_kb,postgres_bytes,spill_backlog_bytes,sim_time_s,pacing_drift_s,health" > "$CSV"

T0=$(date +%s)
start_sim
NEXT_SAMPLE=0 ; KILLED=0 ; PAUSE_DONE=0 ; UNPAUSE_AT=0
while :; do
  ELAPSED=$(( $(date +%s) - T0 ))
  [ "$ELAPSED" -ge "$TOTAL" ] && break
  if ! kill -0 "$PID" 2>/dev/null; then wait "$PID" || true; fail "simulator exited unexpectedly (see $LOG)"; fi
  if [ "$KILLED" = 0 ] && [ "$ELAPSED" -ge "$KILL_AT" ]; then
    sample kill9_before; note "kill -9 pid=$PID"; kill -9 "$PID"; wait "$PID" || true
    KILLED=1; start_sim; sample restarted
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

sample before_sigterm
note "SIGTERM pid=$PID"
kill -TERM "$PID"
set +e; wait "$PID"; CODE=$?; set -e
PID=""
[ "$CODE" = 0 ] || fail "graceful stop exited with $CODE"
note "stopped cleanly (exit 0); reconciling"
uv run vlsim reconcile --run-id "$RUN_ID" --ground-truth-dir "$OUT/gt" > "$OUT/reconcile.json" 2>>"$LOG" \
  || fail "reconcile failed (see $OUT/reconcile.json)"
MAX_RSS=$(awk -F, 'NR > 1 && $5 != "" { if ($5 > m) m = $5 } END { print m }' "$CSV")
note "PASS: ${SOAK_HOURS}h at ${SOAK_SPEED}x, kill -9 + restart and ${PAUSE_SECONDS}s Redpanda pause survived; reconcile ok; max RSS ${MAX_RSS} kB"
echo "PASS" > "$OUT/result.txt"
