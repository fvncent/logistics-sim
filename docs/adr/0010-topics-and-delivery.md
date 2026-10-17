# ADR 0010: Topics, partitions, retention and at-least-once delivery with disk spillover

**Status:** accepted (2026-10-17)

**Context.** The simulator feeds `tracking.scans` and `telemetry.gps` to Redpanda 24/7 on a single home server. If Redpanda is slow or down, the simulator must buffer or slow down; it must never crash or silently drop events (hard rule 7). Retention must not quietly fill the 2 TB disk (SPEC).

**Decision.**

*Topics*
- Keys: `tracking.scans` is keyed by `shipment_id`, `telemetry.gps` by `vehicle_id`. Partitioning uses librdkafka's default partitioner, so every event of one shipment or one vehicle lands in one partition, in order.
- **6 partitions each.** At S/M scale one partition would carry the load (M is about 20 events/s live). Six allow up to six parallel consumers in platform exercises, keep per-key ordering, and stay cheap on a single broker. Changing the count later reshuffles keys, so `vlsim topics` refuses to alter it silently.
- **Retention:** 7 days (`retention.ms = 604800000`, SPEC) plus a size cap per partition (`retention.bytes`).
  - `tracking.scans`: 256 MiB per partition, 1.5 GiB per topic. A week at S is about 21 MB; M is about 4x.
  - `telemetry.gps`: 1 GiB per partition, 6 GiB per topic. A week at S is about 215 MB of JSON; M is about 4x.

  Whichever limit is hit first applies. `cleanup.policy = delete`.
- **Replication factor 1** (single broker). `vlsim topics` (and `open_simulation`) creates missing topics and re-applies retention with incremental config updates.

*Producer*
- `acks=all`, **idempotence off** (see the update below), `linger.ms=50`, lz4 compression, `message.timeout.ms=30000`. All of these live in `config/transport.yaml`, which is **not** part of the config fingerprint: transport tuning never changes *which* events exist, so it must not block a resume.

*Delivery: at-least-once* (`vlsim.emitters.kafka.SpillingEmitter`)
- Every message is tracked until its delivery report arrives. A failure appends it to an on-disk spill file (`<state_dir>/spill.jsonl`).
- Spilling is sticky: while spilled messages are waiting, new messages queue behind them. With more than `max_inflight` (200k) unacknowledged messages, new ones spill too, so memory stays bounded.
- `service()` re-sends the spill when the broker keeps up. With more than `max_spill_bytes` (4 GiB) waiting, the simulation blocks inside librdkafka's poll until the backlog drains (backpressure). It never reads the wall clock.
- At every checkpoint the producer flushes with a timeout (5 s); anything still unacknowledged is spilled and fsynced, and the spill read cursor goes into the checkpoint. Therefore every event emitted before a checkpoint is either acknowledged or durable on disk.
- Duplicates are possible: after a crash, a retried spill, or a late acknowledgement. They carry the same `event_id` (UUIDv5 over kind, counter and content), so consumers and reconcile deduplicate on it.
- Ordering: per-key order holds when the broker is healthy. After an outage, re-sent messages can arrive after newer ones. This is accepted; the platform must handle out-of-order events by `event_time` anyway.

*Run scoping for reconcile*
- A fresh run records each partition's end offset in `<ground_truth>/<run_id>/run.json`.
- `vlsim reconcile` reads from those offsets to the current end and compares **distinct** `event_id`s with ground truth. It also checks that every shipment's last scanned status equals its Postgres `current_status`.

*CI*
- Redpanda runs through `docker compose up redpanda`, because a GitHub service container cannot pass Redpanda's start arguments. Postgres stays a service container.

**Consequences.**
- During an outage the simulation keeps going, and the spill grows on the state volume, up to 4 GiB before backpressure. Afterwards everything is re-sent; the M5 pause test spilled about 158k messages and reconciled with zero loss.
- A checkpoint during an outage costs up to 5 s of wall time (the flush timeout).
- Retention caps are per partition. Re-check them when M scale is measured on the server.

**Update (2026-10-17, M6): idempotence disabled.**
- **What happened.** The M6 soak smoke (kill -9 and restart, then a 30 s `docker pause` of Redpanda, then SIGTERM) failed reconcile: **one scan event was missing** from `tracking.scans`. It was present in ground truth, absent from the spill, and the producer had received a *successful* delivery report for it.
- **Root cause.** During the pause, an in-flight ProduceRequest timed out. When it came back, Redpanda logged `Reseting epoch to 1` for the producer on each partition. The next message librdkafka sent to partition 0 was acknowledged without being stored. This is an interaction between librdkafka 2.16's idempotent-producer epoch bump (KIP-360) and Redpanda v26.1's epoch reset.
- **Fix.** `enable.idempotence: false`, with `acks=all` and effectively unlimited retries. Now a successful delivery report means the broker stored the message, and any failure goes to the spill. The cost is possible duplicates from retries, which are already accepted and deduplicated on `event_id` (baseline artifact B4).
- **Detection.** Reconcile caught this. That is exactly why the soak ends with reconcile and refuses to call a run a pass without it.
