# VietLink Express: Logistics Simulator Documentation

Oct 16, 2026 · @Vince

## Purpose and scope

The simulator is a headless, 24/7 backend that plays the role of a real logistics company's source systems, so you can build and operate a data platform against continuous, realistic, messy data instead of a static dataset.

**What it is**

- A simulated world (depots, vehicles, drivers, merchants, customers, shipments) that evolves over simulated time.
- A thin TMS (transport management system) service that owns a Postgres database and applies business logic, so CDC captures real transactional behavior.
- Emitters for GPS and scan events (Redpanda), carrier files (MinIO), and weather (Open-Meteo).
- A fault injector and a separate ground-truth log, so you can measure whether the platform recovers what really happened.

**What it is not**

- Not a web app. There is no user-facing UI; Grafana provides the monitoring view.
- Not a random-row generator. Delays have causes (weather, congestion, shift limits, carrier reliability) that leave traces in the data.
- Not a routing engine. Distances use haversine × \~1.3 circuity.

**Why build it first:** static datasets cannot teach late-arriving data, schema drift, broker outages, or CDC. A controllable source can, and the generator is itself a portfolio project.

## Company and region

The company is the fictional **VietLink Express**, a Vietnam-wide line-haul plus motorbike last-mile operator on a three-hub north-south corridor. Vietnam was chosen over Singapore and India because it balances regional weather variety (typhoons in the centre, wet/dry split in the south) against modelling effort, and its COD-heavy, messy-address last mile gives data quality work real substance.

| Decision | Choice |
| --- | --- |
| Time zone | UTC+7 |
| Currency | VND (integers, no decimals) |
| Default scale | S for development, M for 24/7 |
| Weather | Open-Meteo history and live, cached to Parquet |
| Distances | Haversine × \~1.3, no routing engine |

**Network** (coordinates approximate; verify before use)

| Code | Role | Lat, Lon | Region |
| --- | --- | --- | --- |
| HAN | North hub (Hanoi) | 21.03, 105.85 | North |
| HPH | Depot (Hai Phong) | 20.86, 106.68 | North |
| VII | Depot (Vinh) | 18.68, 105.68 | North |
| DAD | Central hub (Da Nang) | 16.05, 108.20 | Central |
| HUI | Depot (Hue) | 16.46, 107.59 | Central |
| NHA | Depot (Nha Trang) | 12.24, 109.19 | Central |
| SGN | South hub (Ho Chi Minh City) | 10.78, 106.70 | South |
| BHA | Depot (Bien Hoa) | 10.95, 106.82 | South |
| VCA | Depot (Can Tho) | 10.03, 105.78 | South |

**Lanes:** main line-haul HAN↔DAD and DAD↔SGN (HAN↔SGN direct is rare and slower). Feeders: HPH→HAN, VII→HAN or DAD, HUI→DAD, NHA→DAD or SGN, BHA→SGN, VCA→SGN. Last-mile: motorbike loops of 40-120 stops around each hub and depot.

## System architecture

&#91;embedded content: simulator architecture · 4 outputs, 1 sealed bucket\]

The simulator engine writes business records through the TMS service into Postgres, streams scans and GPS to Redpanda, drops carrier files into MinIO, and keeps its ground-truth bucket sealed off from the platform that consumes the rest.

**Containers:** `sim-engine`, `tms-service`, `tms-postgres`, `redpanda`, `minio`, plus the `prometheus`, `grafana`, and `loki` observability stack. An optional tiny control API (pause, change speed, switch scale, toggle faults) comes after a CLI.

## Simulation engine

The engine is a discrete-event simulation: a sim clock and a priority queue of future events, with a loop that pops the earliest event, advances the clock to it, and runs its handler. Cost scales with events, not with simulated seconds × entities.

**Core loop**

1. Pop the earliest event from the queue.
2. Advance the sim clock to that event's time.
3. Run the handler. It reads world state, current weather, and the seeded RNG, updates state, and schedules new future events.
4. The fault injector may duplicate, delay, drop, or skew the outputs.
5. Outputs go to TMS Postgres, Redpanda, or MinIO, and the true event is written to the ground-truth log.

Handlers chain the business process: `TruckDeparts` computes travel time (distance × congestion × weather factor) and schedules `TruckArrives`.

**Startup**

1. Load YAML config (scale preset, weather effects, fault rates) and seed the RNG.
2. Load the checkpoint if one exists, otherwise build a new world.
3. Load cached weather Parquet, fetching from Open-Meteo only if missing.
4. Schedule recurring events: order arrivals, hourly weather ticks, shift starts, depot cutoffs, daily carrier file drops, fault windows.
5. Start the loop.

**Design rules**

- **Sim clock, not wall clock.** Every event carries an `event_time` from the sim clock. `ingest_time` is stamped downstream, which enables late and out-of-order data practice.
- **Seeded randomness.** The same seed and config must produce byte-identical output.
- **Vectorized GPS.** When a trip starts, all pings for the whole trip are generated in one NumPy batch, each stamped with its sim event time.
- **Weather is world state.** The hourly `WeatherTick` maps cached Open-Meteo rows to a severity class per depot; handlers read it when computing outcomes.
- **Backpressure.** If Redpanda or Postgres slows down, the simulator slows or buffers to disk rather than crashing or dropping silently.
- **Checkpointing.** Queue, entities, and RNG state are saved to SQLite every N sim-minutes so a restart resumes rather than resets.
- **Metrics.** Prometheus exposes sim time, events/s, queue depth, and fault counts; a health endpoint is included.

**Run modes (same handlers in both)**

| Mode | Clock | Writes | Use |
| --- | --- | --- | --- |
| Live | Tracks wall clock × speed knob; sleeps until next event | Postgres app writes, Redpanda topics, MinIO file drops | 24/7 operation, demos, CDC |
| Backfill | Runs 100-1000× faster, no sleeping | Bulk Parquet straight to bronze | Build 6-12 months of history, then hand off to live |

## Realism model layers

Data looks real when it has causal structure: delays have reasons, and the reasons leave traces. The model is built in five layers, with every parameter in versioned YAML.

| Layer | What it models |
| --- | --- |
| 1. Network and geography | Three hubs, six depots, last-mile zones; line-haul lanes and motorbike loops; distance = haversine × \~1.3 |
| 2. Demand | Non-homogeneous Poisson order arrivals: hour-of-day × day-of-week × annual seasonality (11.11, 12.12, Tet). Heavy-tailed merchants (about 20% of merchants produce 80% of volume). Weight log-normal; service level and distance correlated |
| 3. Capacity and operations | Vehicle types and capacity; driver shift and hours limits; depot dock doors as a queue so congestion emerges; sorting cutoffs (miss it and the shipment slips a day) |
| 4. Movement | Speed by road class × time-of-day congestion × weather factor; GPS position with noise and device imperfections |
| 5. Carriers, rates, money | Own and partner carriers with reliability tiers; rate cards (per kg, per km, minimums); fuel surcharge from a fuel price series; invoices generated from what happened, mostly matching and occasionally disagreeing |

**Cash on delivery.** COD is a first-class flow: `cod_collections` (cash taken by drivers) versus `cod_remittances` (cash remitted by depots). They should match and sometimes will not, which creates a built-in reconciliation problem.

**Addresses.** Vietnamese administrative divisions were, I believe, restructured in 2025 with the district level removed. Verify this before modelling. If it holds, generate addresses in both old and new formats and let the mapping drift over time as a master-data and schema-change exercise.

## Weather model

Weather is a causal input to the simulation, not a decoration: real hourly weather per depot is mapped to a severity class, and handlers apply it through several channels.

**Source.** Open-Meteo historical archive for backfill and its forecast/current API for live mode, for each depot's coordinates (temperature, precipitation, wind gusts, visibility, weather code). Cache to local Parquet so each range is fetched once. No API key is needed, but fair-use limits and non-commercial terms apply; check current terms before depending on it.

**Severity classes:** light rain, heavy rain, flooding, typhoon warning (plus clear). Regions: north (HAN, HPH, VII), central (DAD, HUI, NHA), south (SGN, BHA, VCA).

**Effect config (modelling assumptions)**

```yaml
weather_effects:
  speed_multiplier:   {heavy_rain: 0.6, typhoon_warning: 0.3, light_rain: 0.9}
  delivery_fail_prob: {base: 0.08, heavy_rain: +0.12, typhoon_warning: +0.35}
  depot_throughput:   {flooding: 0.5}
  demand_multiplier:  {heavy_rain_b2c: 1.15}
  breakdown_hazard:   {heavy_rain: 1.8}
regions: {north: [HAN,HPH,VII], central: [DAD,HUI,NHA], south: [SGN,BHA,VCA]}
```

| Channel | Effect |
| --- | --- |
| Driving speed | Slower in rain, fog, typhoon conditions |
| Breakdown and accident risk | Hazard rate rises with severity |
| Depot throughput | Flooding slows loading; dock queues grow |
| Delivery failure | Failed attempts and reschedules rise |
| Fuel use | Higher in adverse conditions |
| Demand | Bad weather raises B2C order volume |

**Honesty note.** The weather itself is real; the effect sizes are my modelling assumptions, not measured logistics data. Keep them in config, record them in an ADR as assumptions, and never present them as empirical findings. Because you know the planted effect sizes, you can test whether the platform recovers them.

## TMS schema and event schemas

The TMS Postgres database is the OLTP source system. The TMS service updates `status`, `updated_at`, and `attempt_count` in place, which is what makes CDC meaningful. Keep it as a separate Postgres instance from the platform's own databases.

**Core tables**

```sql
depots(depot_id, code, name, type[hub|depot], lat, lon, region, dock_doors)
merchants(merchant_id, name, tier[enterprise|mid|small], cod_share, created_at)
customers(customer_id, name_masked, phone_hash, created_at)
addresses(address_id, customer_id, province, district, ward, street_raw,
          lat, lon, quality[good|ambiguous|bad])
orders(order_id, merchant_id, customer_id, address_id, created_at,
       service_level[standard|express], cod_amount_vnd, weight_kg, status)
shipments(shipment_id, order_id, origin_depot, dest_depot, current_status,
          promised_at, delivered_at, attempt_count, carrier_id, updated_at)
vehicles(vehicle_id, type[motorbike|van|truck], model, home_depot, capacity_kg)
drivers(driver_id, home_depot, shift_start, shift_hours_limit)
trips(trip_id, vehicle_id, driver_id, from_depot, to_depot, planned_dep,
      actual_dep, planned_arr, actual_arr, kind[linehaul|lastmile])
trip_shipments(trip_id, shipment_id, stop_seq)
carriers(carrier_id, name, type[own|partner], reliability_tier)
rate_cards(rate_id, carrier_id, lane, per_kg_vnd, per_km_vnd, min_charge_vnd,
           valid_from, valid_to)
cod_collections(collection_id, shipment_id, driver_id, amount_vnd, collected_at)
cod_remittances(remit_id, depot_id, amount_vnd, remitted_at, status)
invoices / invoice_lines(carrier_id, shipment_id, charge_type, amount_vnd, ...)
```

**Event schemas** (Redpanda; key = `shipment_id` or `vehicle_id`; `ingest_time` is stamped downstream)

```json
// topic: tracking.scans (key: shipment_id)
{"event_id":"uuid","schema_v":1,"shipment_id":"S-83110442",
 "event_type":"OUT_FOR_DELIVERY","depot":"DAD",
 "event_time":"2026-10-16T09:41:12+07:00","device_id":"HH-0412",
 "driver_id":"D-2210","reason_code":null}

// topic: telemetry.gps (key: vehicle_id)
{"event_id":"uuid","vehicle_id":"V-0192","event_time":"...",
 "lat":16.0544,"lon":108.2022,"speed_kmh":31.5,
 "fuel_pct":64.2,"device_seq":88123}
```

`device_seq` lets the platform detect gaps and duplicates. Topics use roughly 7-day retention.

## Shipment state machine

&#91;embedded content: shipment state machine · 10 states, 1 retry loop\]

A shipment moves through line-haul legs (looping at hubs for additional legs), then out for delivery, and ends Delivered or Returned after at most three attempts.

Transitions are driven by queues and capacity, not coin flips: line-haul departure waits for the sorting cutoff and truck fill, and out-for-delivery waits for rider availability and shift limits. Delivery outcomes are sampled from a probability that depends on weather, address quality, and COD. Scan events use upper-case names such as `OUT_FOR_DELIVERY`.

**Text version of the diagram** (the drawing does not survive export, so build from this table)

The ten states are `CREATED`, `PICKED_UP`, `AT_ORIGIN_DEPOT`, `LINEHAUL_DEPARTED`, `AT_HUB`, `AT_DEST_DEPOT`, `OUT_FOR_DELIVERY`, `DELIVERED`, `DELIVERY_FAILED`, and `RETURNED`. `EXCEPTION` is a side exit. There is no separate in-transit-return state: a failed final attempt goes straight to `RETURNED`.

| From | To | Condition |
| --- | --- | --- |
| `CREATED` | `PICKED_UP` | A rider collects the parcel |
| `PICKED_UP` | `AT_ORIGIN_DEPOT` | Arrives at the origin depot and joins the dock queue |
| `AT_ORIGIN_DEPOT` | `LINEHAUL_DEPARTED` | Makes the sorting cutoff and truck fill; otherwise slips to the next day |
| `LINEHAUL_DEPARTED` | `AT_HUB` | Truck arrives at a hub (travel time includes congestion and weather) |
| `AT_HUB` | `LINEHAUL_DEPARTED` | Another line-haul leg is needed |
| `AT_HUB` | `AT_DEST_DEPOT` | Final line-haul leg completes |
| `AT_DEST_DEPOT` | `OUT_FOR_DELIVERY` | A rider is available within shift limits |
| `OUT_FOR_DELIVERY` | `DELIVERED` | Attempt succeeds (a COD collection row is created) |
| `OUT_FOR_DELIVERY` | `DELIVERY_FAILED` | Attempt fails; probability depends on weather, address quality, and COD |
| `DELIVERY_FAILED` | `OUT_FOR_DELIVERY` | Reschedule while `attempt_count` is below 3 |
| `DELIVERY_FAILED` | `RETURNED` | The third attempt has failed |
| Any non-terminal state | `EXCEPTION` | Damage, lost, or `address_unresolvable` |

Terminal states are `DELIVERED` and `RETURNED`. What happens after `EXCEPTION` (resolved and resumed, or written off) is not yet specified; decide it and record the choice in `docs/ASSUMPTIONS.md`.

## Planted patterns and ground truth

Eight documented patterns are hidden in the data for the platform and agents to discover. The answer key lives in a separate bucket with different credentials, and the platform must never read it. It is used for validation and for scoring the LLM agents.

| # | Pattern |
| --- | --- |
| 1 | Central-region depots show far higher delays in typhoon-season weather than north or south |
| 2 | Partner carrier B is about 12% less on-time on the DAD→SGN lane |
| 3 | COD failure and return rates are higher for ambiguous addresses |
| 4 | Depot BHA degrades on Monday mornings (weekend backlog) |
| 5 | Carrier C over-bills fuel surcharge by about 3% on average |
| 6 | Peak-season delays appear about a week before volume peaks (11.11, 12.12, Tet) |
| 7 | One vehicle model has a higher breakdown rate |
| 8 | COD remittance shortfalls cluster at two specific depots |

**Ground-truth log.** The simulator records what it actually emitted: counts, true delivery times, and every injected fault with an ID, type, and time. The pipeline is reconciled against it (for example, "the generator sent 98,412 scans; gold shows 98,412 after dedup"), and the investigator agent is scored by injecting known faults.

## Fault catalog and injector

The fault injector wraps every emitter. After a handler produces an output, it rolls configured fault rates and may duplicate, delay (by re-queuing the emission for a later sim time), drop, or corrupt it. The TMS record and ground truth stay correct; the stream the platform sees is the messy version. Reconciling the two is the data engineering work. Every action is logged with a fault ID.

| Category | Faults |
| --- | --- |
| Events | Duplicate scans (retries), out-of-order arrival, late by hours or days, missing milestone |
| Devices | GPS jitter, dropouts, offline buffer-and-flush bursts, clock skew |
| Master data | Province, district, and ward spelling variants; address format changes; vehicle reassignment; manual UPDATEs and occasional DELETEs in the TMS |
| Carrier files | Renamed or reordered columns, new field, date format change, duplicate or empty file, encoding errors |
| System | Broker outage, Postgres failover, bad rate-card update, schema change |

Fault rates are configurable and scheduled by config. Build the injector in tiers: the MVP has none, v2 adds duplicates and lateness, and v3 adds the full catalog and a fault schedule.

## Scaling, resources, and storage

One config knob sets world size, and everything (depots, vehicles, merchants, order rate) derives from it. The figures below are my estimates (about 12 active hours per day, one GPS ping per 10 s per vehicle); benchmark early rather than trust them.

| Scale | Vehicles | Shipments/day | GPS events/day | Live rate | Use |
| --- | --- | --- | --- | --- | --- |
| S | 50 | \~1,200 | \~220k | \~5/s | Development, CI |
| M (default) | 200 | \~5,000 | \~860k | \~20/s | Normal 24/7 operation |
| L | 2,000 | \~50,000 | \~8.6M | \~200/s | Load testing |
| XL | 20,000 | \~500,000 | \~86M | \~2,000/s | Short stress bursts only |

**How it scales**

- Discrete-event queue, so cost follows events.
- Shard by region or depot as independent processes; cross-shard handoffs via messages.
- Batch emission to Redpanda; Postgres `COPY` or multi-row inserts for non-transactional data.
- Backpressure and disk buffering instead of crashes.
- Backfill mode vectorizes GPS with NumPy and writes Parquet directly.

**Hardware budget (Ryzen 5 5600, 16 GB RAM, 2 TB)**

RAM is the bottleneck, not storage or CPU. The simulator needs well under 0.5 GB at S and M scale and runs always-on in its own `sim` Compose profile. Run `core + streaming` or `core + ai`, never all at once. Upgrading to 32 GB is the best first purchase.

**Where data lives (Docker bind mounts on the 2 TB drive)**

| Data | Location |
| --- | --- |
| Orders, shipments, customers, carriers, invoices | TMS Postgres (OLTP; Debezium reads the WAL) |
| GPS and scan events | Redpanda topics, disk-backed, \~7-day retention |
| Carrier files | MinIO `landing/` bucket (mimics SFTP drop) |
| Weather | Local Parquet cache, then bronze |
| Simulator state and checkpoints | SQLite file, internal to the simulator |
| Ground truth and answer key | Separate MinIO bucket with different credentials |
| Bronze, silver, gold | MinIO (Parquet or Iceberg) |
| Backups | External disk plus cloud bucket (restic, 3-2-1) |

Rough sizing at M scale: a year of raw GPS is about 300M events, around 40-50 GB as JSON and 6-10 GB as Parquet. Set retention on Redpanda and Docker logs, since those quietly fill disks.

## Validation and testing

Validate the simulator itself before trusting the platform built on it.

- **Reproducibility:** the same seed and config produce byte-identical output. Run this in CI.
- **Conservation:** every created shipment reaches a terminal state; vehicle, driver, and parcel counts stay consistent.
- **Statistical sanity:** weights, transit times, and delivery rates fall within plausible ranges. On-time rates in the high 80s to mid 90s are a sanity band, not a verified benchmark.
- **Queueing sanity:** depot waits rise sharply as utilization nears 100%. If they do not, the dock model is wrong.
- **Calibration loop:** tune parameters until KPIs (on-time %, cost per km, dwell time) land in plausible bands, then freeze and version the config.
- **Ground-truth reconciliation:** emitted counts in the ground-truth log must match what Postgres and the topics received, before faults are applied.
- **Pattern recovery:** once the platform exists, check that it rediscovers the eight planted patterns.

## Repo structure, build phases, and MVP exit

The MVP is done when S scale runs 24 hours with no crashes and the ground-truth log matches what the TMS and topics received. Only the MVP blocks starting the batch pipeline; the rest grows alongside the platform.

```
logistics-sim/
├── config/       # scale presets, weather effects, fault rates (versioned YAML)
├── world/        # depots, lanes, merchants, customers, vehicles, carriers
├── engine/       # event queue, sim clock, shards, state persistence
├── models/       # demand, movement, weather, depot queues, billing
├── emitters/     # postgres_app, kafka_gps, kafka_scans, file_dropper
├── faults/       # injector, fault catalog, ground-truth writer
├── weather/      # open-meteo fetcher and local cache
├── backfill/     # fast vectorized parquet writer
├── validation/   # conservation, statistical, reproducibility checks
├── docs/         # model assumptions, ADRs; answer key kept separate
└── tests/
```

| Phase | Weeks | Scope | Gate |
| --- | --- | --- | --- |
| 1. MVP | 1-2 | Config and S preset, depots and lanes, Postgres schema and migrations, event queue, sim clock, seeded RNG, order generator with seasonality, shipment state machine, TMS service, scan emitter, basic GPS, speed knob, Prometheus metrics | 24-hour S run, ground truth matches receipts, same seed gives identical output |
| 2. Realism | 3-4 | Weather fetcher and cache, weather speed and dwell channels, depot queues, ground-truth log | Weather effects visible in output; queues rise near full utilization |
| 3. Money and patterns | 5-6 | Carriers, rate cards, invoices, COD flows, planted patterns, backfill mode | Backfill of months of history completes; patterns present in data |
| 4. Faults and scale | alongside platform | Fault catalog and schedule, sharding for L and XL, validation checks in CI | Fault runs logged; L scale sustained |

First two weeks in detail: days 1-3 repo, config, depots and lanes, schema and migrations; days 4-7 event queue, clock, RNG, order generator; days 8-11 state machine, TMS service, scan emitter; days 12-14 GPS pings, speed knob, metrics, reproducibility test.

## Open questions and verify-before-use

- [ ] Depot and hub coordinates are approximate; verify against a map source before use.
- [ ] Vietnam's administrative restructure (district level removed in 2025) is from memory; confirm the current province and ward structure before modelling addresses.
- [ ] Open-Meteo terms and rate limits for non-commercial use; confirm current terms.
- [ ] All weather effect sizes and planted-pattern magnitudes are modelling assumptions; record them in an ADR.
- [ ] Throughput and sizing figures are estimates; benchmark on the target machine.
- [ ] Decide whether to develop on the MacBook first (see the Claude Code prompt doc) and when to move to the Ubuntu server.
- [ ] Tooling choices still open: Dagster versus Airflow for the platform, Alembic versus plain SQL for migrations.
