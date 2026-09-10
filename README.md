# EV Charger Monitor

Lightweight OCPP 1.6 telemetry monitor using FastAPI and SQLite.

## Run locally

### Windows PowerShell

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open the dashboard at [http://localhost:8000](http://localhost:8000).

The SQLite database is created at `data/ev_monitor.db`.

Configure each charger to connect using:

```text
ws://<computer-ip>:8000/<charger_id>
```

For example: `ws://192.168.1.10:8000/CP001`.

## Railway deployment

1. Push this project to GitHub.
2. Create a new Railway project from the GitHub repository.
3. Add a Railway Volume and mount it at `/data` for persistent SQLite storage.
4. Deploy. Railway provides the required `PORT` automatically.
5. Set each charger WebSocket URL to:

```text
wss://<your-railway-domain>/<charger_id>

wss://autel-ocpp-production.up.railway.app/CP001
```

The dashboard and API are available at the same Railway domain.

## Useful endpoints

```text
GET /health
GET /api/chargers
GET /api/events
GET /api/sessions
GET /api/export/events?format=csv
```

No charger control functionality is implemented.

## Run tests

With the virtual environment activated:

```powershell
python -m unittest discover -s tests -v
```

These tests do not connect to a real charger. They verify the local app, SQLite initialization, OCPP response classes, routes, and WebSocket adapter.
