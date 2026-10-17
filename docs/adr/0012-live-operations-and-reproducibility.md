# ADR 0012: Live operations, health, graceful stop, and reproducibility per platform

**Status:** accepted (2026-10-17)

**Decision.**

*Live mode*
- `vlsim run` drives `Simulation` through `vlsim.engine.live.LiveRunner`. Its `Pacer` maps sim time to wall time with a speed knob from 1x to 1000x, settable at runtime with `POST /speed?x=N`.
- `live.py` is the only module that reads or sleeps on the wall clock. It sleeps in chunks of at most 0.25 s, so a stop request is honoured quickly. Each step advances to the next event or checkpoint boundary.
- Wall time decides only *when* things happen: pacing, the 1 s flush cadence (ADR 0009), retries and metrics. It never decides *what* happens, so a live run produces the same content as a fast run.
- Pacing drift (`vlsim_pacing_drift_seconds`) is how far the runner is behind the wall-clock schedule.

*Errors and health*
- Postgres errors (OperationalError, InterfaceError) during flush or checkpoint are retried with exponential backoff, up to 30 s. Nothing is released before a commit, so a retry is safe.
- `/health` on :8081 reports:
  - `failing` (503) when Postgres is unreachable or flushes are failing;
  - `degraded` (200) when Redpanda is unreachable or the producer has a spill backlog;
  - `ok` otherwise.
- Metrics (Prometheus, own registry): sim time, engine events (counter and per wall second), queue depth, emitted events per topic, spill backlog bytes, flush lag (sim and wall), checkpoint age (sim and wall), pacing drift and speed.

*Graceful stop*
- SIGTERM or SIGINT sets a flag. The runner finishes its current step, then `Simulation.close()`:
  1. commits Postgres;
  2. releases events to ground truth and the producer;
  3. seals completed days and fsyncs;
  4. flushes the producer, spilling whatever is unacknowledged;
  5. saves the checkpoint.
- The process exits 0. In Compose, `stop_grace_period: 2m`.
- `kill -9` gets no handler: the restart resumes from the last checkpoint and replays (ADRs 0008, 0011).

*Ground-truth sealing*
- At each checkpoint, day files dated before the current local sim date are gzipped reproducibly (mtime 0, no name). Truncate-on-resume handles plain files, sealed files, a seal made after the checkpoint (it unpacks it) and a crash mid-seal (it removes the `.tmp`).

*Reproducibility*
- The output hashes from `tests/repro_run.py` (S, 1 sim day: scans, GPS, TMS snapshot, engine checkpoint) must match between two fresh interpreters, and match the golden for `<OS>-<arch>` in `tests/golden/repro_hashes.json`. CI runs this on every PR and push to `main`.
- **Determinism is guaranteed per OS and architecture, not across them.** numpy and libm transcendental functions may round differently per CPU architecture, SIMD path and C library. One differing last bit can move a rounded coordinate or a millisecond and change every later event.
- On 2026-10-17 Darwin-arm64, Linux-aarch64 and Linux-x86_64 (the latter under QEMU emulation) all produced the same hashes. That is recorded but not promised. If CI's real x86 hardware ever disagrees with the emulated value, update the Linux-x86_64 golden from the CI log; the test prints it.

**Consequences.**
- A restart (graceful or not) never resets the world. Ground truth and topics reconcile afterwards; tested with real `vlsim run` processes against SIGTERM and kill -9.
- Golden hashes change whenever a model change is intended. Update them deliberately in the same commit.
