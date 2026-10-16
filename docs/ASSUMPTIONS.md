# Modelling and structural assumptions

These are my assumptions, not measured data. Each one lives in config where possible and should be revisited during calibration.

| # | Assumption | Where | Added |
|---|---|---|---|
| A1 | Package layout is `src/vlsim/<area>/` rather than the spec's top-level `world/`, `engine/`, … dirs (avoids import-name collisions such as `engine` and `models`). Approved 2026-10-16. | repo | M1 |
| A2 | Depot coordinates are the spec's approximate values, unverified against a map source. | `config/network.yaml` | M1 |
| A3 | Dock doors: 12 (HAN), 10 (DAD), 14 (SGN), 4 at each depot. | `config/network.yaml` | M1 |
| A4 | Lanes are undirected: every listed lane runs both ways (the spec draws feeders one way, but outbound delivery needs hub→depot). | `config/network.yaml` | M1 |
| A5 | S preset: 36 motorbikes, 8 vans, 6 trucks (50 total, as in the spec), 60 drivers, 200 merchants, 5,000 customers. M preset: 150/30/20 (200 total), 240 drivers, 800 merchants, 20,000 customers. Only the vehicle totals and shipments/day come from the spec. | `config/scale/` | M1 |
| A6 | Sim start 2026-10-01T00:00+07:00, seed 20261016. | `config/base.yaml` | M1 |
| A7 | Addresses use the post-2025 two-tier format (province, ward). `district` is nullable and left empty in the MVP. Ward names are synthetic labels from config, not real wards. The 2025 restructure (63→34 provinces, district level removed) is from memory and still needs checking. | M2 seed | M1 |
| A8 | Shipment states: CREATED, PICKED_UP, AT_ORIGIN_DEPOT, LINEHAUL_DEPARTED, AT_HUB (loops back to LINEHAUL_DEPARTED), AT_DEST_DEPOT, OUT_FOR_DELIVERY, DELIVERY_FAILED (reschedule to OUT_FOR_DELIVERY, max 3 attempts, then RETURNED), DELIVERED, RETURNED, EXCEPTION (damage, lost, address_unresolvable; can interrupt any stage). Confirmed by Vince 2026-10-16. EXCEPTION is terminal in the MVP (ADR 0005). | M4 | M1 |
| A9 | `telemetry.gps` events carry `schema_v` even though the spec's example omits it (the build prompt requires it on every event). | M5 | M1 |
| A10 | Tests use FastAPI 0.143.3 / Starlette 1.8.0 with httpx 0.28.1. Starlette's warning to switch TestClient to `httpx2` is filtered narrowly in `pyproject.toml`; `httpx2` is not adopted (decision 2026-10-16). Revisit when upgrading FastAPI. | `pyproject.toml` | M1 |
| A11 | TMS Postgres host port comes from `TMS_PG_PORT` (default 55432), read by both Compose and `vlsim.tms.settings`; `VLSIM_TMS_DSN` overrides it. | `.env.example` | M1 |
