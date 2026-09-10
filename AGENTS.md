# AGENTS.md — EV Charger Monitoring Platform (DLE)

## Objective
Lightweight **telemetry-only** EV charger monitoring platform.

**Phase 1 = Monitoring Only.**
Do NOT implement: smart charging, remote start/stop, load balancing, charging profiles, or any charger control. This is strictly OCPP data collection + visualisation.

## Environment
- Chargers: 3x Autel Maxi EU AC W22-C5-4G-DG
- Connectivity: WiFi, SSID `DLE-EV`
- Protocol: OCPP 1.6, charger connects out to our WebSocket endpoint

## Architecture
```
Autel Charger --OCPP 1.6 WebSocket--> FastAPI --> SQLite (source of truth)
                                          |
                                          +--> REST API --> Dashboard / Digital Twin / Power BI
```

## Stack
Python 3.12+, FastAPI, Uvicorn, `python-ocpp` (installed via pip, **not** cloned from source — `from ocpp.v16 import ChargePoint`).

## Database: SQLite only
- No Postgres/MySQL/SQL Server/MongoDB/Redis. Single-file, zero-config, easy to back up/export/migrate.
- Path resolution priority: (1) Railway volume mount, (2) configured data dir, (3) local app data folder. Default: `data/ev_monitor.db`.
- Storage layer must stay isolated from OCPP processing logic, so future migration to Postgres/Azure SQL/Data Lake needs no changes to OCPP handling.
- Suggested tables: `chargers`, `events`, `meter_values`, `sessions` (schema may evolve).
- Retain **all** raw OCPP payloads, including unknown message types — never discard.

## OCPP Messages to Handle
| Message | Store |
|---|---|
| BootNotification | charger_id, vendor, model, firmware, timestamp |
| Heartbeat | charger_id, timestamp (used for online/offline detection) |
| StatusNotification | charger_id, status, timestamp, raw_payload (Available/Preparing/Charging/SuspendedEVSE/SuspendedEV/Finishing/Faulted/Unavailable) |
| MeterValues | full raw payload + parsed fields (Power, Voltage, Current, Energy, Frequency, PowerFactor) |
| StartTransaction | transaction_id, charger_id, start_time |
| StopTransaction | transaction_id, charger_id, end_time, energy |

Note: OCPP is not REST — charger holds a persistent WebSocket connection (`wss://host/{charger_id}`), it never calls our REST endpoints.

## WebSocket Endpoint
`@app.websocket("/{charger_id}")` — e.g. `/CP001`. Unknown charger_id → auto-register.

## REST API (for Digital Twin / Power BI / dashboard / manual inspection — chargers never call these)
- `GET /health` → `{"status":"ok"}`
- `GET /api/chargers` — list with status + last_seen
- `GET /api/chargers/{charger_id}` — latest known state
- `GET /api/chargers/{charger_id}/latest` — latest MeterValues
- `GET /api/sessions` — session history (from Start/StopTransaction)
- `GET /api/events?limit=100&charger_id=&message_type=` — recent raw events, default latest 100, queried directly from SQLite
- Exports (JSON + CSV, e.g. `?format=csv`):
  - `GET /api/export/events`
  - `GET /api/export/meter-values`
  - `GET /api/export/sessions`

## Dashboard
Single page, no auth needed for PoC.
- **Home:** Total Chargers, Online Chargers, Active Sessions, Energy Today (cards)
- **Charger table:** Charger ID, Status, Last Power, Last Energy Reading, Last Seen, Current Session
- **Charger detail:** latest status, recent events, recent meter readings (charts optional)

## Logging
Log connections, disconnections, Heartbeat, MeterValues, BootNotification, StatusNotification, and errors to standard application logs.

## Deployment (Railway)
- Listen on `process.env.PORT` (`port = int(os.getenv("PORT", 8000))`)
- Must support persistent WebSocket connections
- No charger auth / no dashboard login for now. Future (not now): API keys, JWT, OAuth, OCPP security profiles.

## Explicitly Out of Scope (Phase 2, reserved only — do not build)
RemoteStartTransaction, RemoteStopTransaction, SetChargingProfile, Load Balancing, Dynamic Tariffs, Solar Integration, EVCC Integration.

## Definition of Done
1. Autel charger connects successfully.
2. BootNotification received.
3. Heartbeats received.
4. MeterValues received.
5. Data written to SQLite.
6. Dashboard shows live charger info.
7. REST APIs expose telemetry.
8. Digital Twin can call REST APIs.
9. Deploys successfully to Railway.
10. No charger control functionality exists.
