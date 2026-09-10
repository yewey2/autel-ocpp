import csv
import io
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call_result
from ocpp.v16.enums import Action, RegistrationStatus

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ev-monitor")

DATA_DIR = os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else "data")
DB_PATH = os.path.join(DATA_DIR, "ev_monitor.db")
app = FastAPI(title="EV Charger Monitoring")

def now(): return datetime.now(timezone.utc).isoformat()

def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with closing(db()) as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS chargers (charger_id TEXT PRIMARY KEY, vendor TEXT, model TEXT, firmware TEXT, status TEXT, last_seen TEXT, last_boot TEXT);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, charger_id TEXT, message_type TEXT, timestamp TEXT, raw_payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meter_values (id INTEGER PRIMARY KEY AUTOINCREMENT, charger_id TEXT, timestamp TEXT, power REAL, voltage REAL, current REAL, energy REAL, frequency REAL, power_factor REAL, raw_payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (transaction_id INTEGER PRIMARY KEY, charger_id TEXT, start_time TEXT, end_time TEXT, start_meter REAL, end_meter REAL, energy REAL);
        """)
        c.commit()

def record_event(c, charger_id, message_type, payload):
    c.execute("INSERT INTO events(charger_id,message_type,timestamp,raw_payload) VALUES(?,?,?,?)", (charger_id, message_type, now(), json.dumps(payload, default=str)))
    c.execute("INSERT INTO chargers(charger_id,last_seen) VALUES(?,?) ON CONFLICT(charger_id) DO UPDATE SET last_seen=excluded.last_seen", (charger_id, now()))

def reading(items, key):
    for x in items or []:
        if x.get("measurand", "Energy.Active.Import.Register") == key or (key == "Energy.Active.Import.Register" and x.get("measurand") in (None, "Energy.Active.Import.Register")):
            try: return float(x.get("value"))
            except (TypeError, ValueError): return None
    return None

class FastAPIWebSocketAdapter:
    """Adapter from FastAPI WebSocket methods to python-ocpp's interface."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

    async def recv(self):
        return await self.websocket.receive_text()

    async def send(self, message):
        await self.websocket.send_text(message)


class Charger(OcppChargePoint):
    def __init__(self, charger_id, connection): super().__init__(charger_id, connection); self.charger_id = charger_id
    def event(self, typ, payload):
        with closing(db()) as c:
            record_event(c, self.charger_id, typ, payload); c.commit()
        log.info("%s %s", self.charger_id, typ)

    @on(Action.boot_notification)
    async def boot_notification(self, charge_point_vendor, charge_point_model, firmware_version=None, **kwargs):
        p = {"chargePointVendor": charge_point_vendor, "chargePointModel": charge_point_model, "firmwareVersion": firmware_version, **kwargs}; self.event("BootNotification", p)
        with closing(db()) as c:
            c.execute("UPDATE chargers SET vendor=?,model=?,firmware=?,status=?,last_boot=? WHERE charger_id=?", (charge_point_vendor, charge_point_model, firmware_version, "Available", now(), self.charger_id)); c.commit()
        return call_result.BootNotificationPayload(current_time=now(), interval=60, status=RegistrationStatus.accepted)

    @on(Action.heartbeat)
    async def heartbeat(self, **kwargs): self.event("Heartbeat", kwargs); return call_result.HeartbeatPayload(current_time=now())

    @on(Action.status_notification)
    async def status_notification(self, connector_id, error_code, status, **kwargs):
        p = {"connectorId": connector_id, "errorCode": error_code, "status": status, **kwargs}; self.event("StatusNotification", p)
        with closing(db()) as c: c.execute("UPDATE chargers SET status=? WHERE charger_id=?", (status, self.charger_id)); c.commit()
        return call_result.StatusNotificationPayload()

    @on(Action.meter_values)
    async def meter_values(self, connector_id, meter_value, **kwargs):
        p = {"connectorId": connector_id, "meterValue": meter_value, **kwargs}; self.event("MeterValues", p)
        for sample in meter_value:
            values = sample.get("sampledValue", []); ts = sample.get("timestamp", now())
            with closing(db()) as c:
                c.execute("INSERT INTO meter_values(charger_id,timestamp,power,voltage,current,energy,frequency,power_factor,raw_payload) VALUES(?,?,?,?,?,?,?,?,?)", (self.charger_id, ts, reading(values,"Power.Active.Import"), reading(values,"Voltage"), reading(values,"Current.Import"), reading(values,"Energy.Active.Import.Register"), reading(values,"Frequency"), reading(values,"Power.Factor"), json.dumps(sample, default=str))); c.commit()
        return call_result.MeterValuesPayload()

    @on(Action.start_transaction)
    async def start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        p={"connectorId":connector_id,"idTag":id_tag,"meterStart":meter_start,"timestamp":timestamp,**kwargs}; self.event("StartTransaction",p)
        with closing(db()) as c: c.execute("INSERT OR REPLACE INTO sessions(transaction_id,charger_id,start_time,start_meter) VALUES(?,?,?,?)", (0, self.charger_id, timestamp, meter_start)); c.commit()
        return call_result.StartTransactionPayload(transaction_id=0, id_tag_info={"status":"Accepted"})

    @on(Action.stop_transaction)
    async def stop_transaction(self, transaction_id, meter_stop, timestamp, **kwargs):
        p={"transactionId":transaction_id,"meterStop":meter_stop,"timestamp":timestamp,**kwargs}; self.event("StopTransaction",p)
        with closing(db()) as c: c.execute("UPDATE sessions SET end_time=?,end_meter=?,energy=end_meter-start_meter WHERE transaction_id=?", (timestamp,meter_stop,transaction_id)); c.commit()
        return call_result.StopTransactionPayload(id_tag_info={"status":"Accepted"})

@app.on_event("startup")
def startup(): init_db()

@app.get("/health")
def health(): return {"status":"ok"}

@app.websocket("/{charger_id}")
async def websocket(websocket: WebSocket, charger_id: str):
    # OCPP 1.6 over WebSocket uses the `ocpp1.6` subprotocol.  The adapter
    # converts FastAPI's receive_text/send_text methods to recv/send, which
    # are the methods expected by python-ocpp.
    requested = websocket.headers.get("sec-websocket-protocol", "")
    subprotocol = "ocpp1.6" if "ocpp1.6" in requested else None
    await websocket.accept(subprotocol=subprotocol)
    log.info("connected %s (subprotocol=%s)", charger_id, subprotocol or "none")
    cp = Charger(charger_id, FastAPIWebSocketAdapter(websocket))
    try: await cp.start()
    except WebSocketDisconnect: pass
    except Exception: log.exception("OCPP error for %s", charger_id)
    finally: log.info("disconnected %s", charger_id)

def rows(sql, args=()):
    with closing(db()) as c: return [dict(x) for x in c.execute(sql,args).fetchall()]

@app.get("/api/chargers")
def chargers(): return rows("SELECT * FROM chargers ORDER BY charger_id")
@app.get("/api/chargers/{charger_id}")
def charger(charger_id: str): return rows("SELECT * FROM chargers WHERE charger_id=?",(charger_id,))[0] if rows("SELECT * FROM chargers WHERE charger_id=?",(charger_id,)) else JSONResponse({"error":"not found"},404)
@app.get("/api/chargers/{charger_id}/latest")
def latest(charger_id: str): return rows("SELECT * FROM meter_values WHERE charger_id=? ORDER BY id DESC LIMIT 1",(charger_id,))
@app.get("/api/sessions")
def sessions(): return rows("SELECT * FROM sessions ORDER BY start_time DESC")
@app.get("/api/events")
def events(limit: int=Query(100,le=1000), charger_id: str|None=None, message_type: str|None=None):
    sql="SELECT * FROM events WHERE 1=1"; args=[]
    if charger_id: sql += " AND charger_id=?"; args.append(charger_id)
    if message_type: sql += " AND message_type=?"; args.append(message_type)
    return rows(sql+" ORDER BY id DESC LIMIT ?", args+[limit])

@app.get("/api/export/{kind}")
def export(kind: str, format: str="json"):
    table = {"events":"events","meter-values":"meter_values","sessions":"sessions"}.get(kind)
    if not table: return JSONResponse({"error":"unknown export"},400)
    data=rows(f"SELECT * FROM {table} ORDER BY id DESC")
    if format.lower() != "csv": return data
    out=io.StringIO(); w=csv.DictWriter(out, fieldnames=data[0].keys() if data else ["message"]); w.writeheader(); w.writerows(data)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition":f"attachment; filename={kind}.csv"})

@app.get("/", response_class=HTMLResponse)
def dashboard(): return HTML

HTML = """<!doctype html><title>EV Monitor</title><meta name=viewport content='width=device-width,initial-scale=1'><style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#f5f7fa;color:#17202a}main{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem}.card,section{background:white;padding:1rem;border-radius:12px;box-shadow:0 1px 5px #ccd}table{width:100%;border-collapse:collapse}td,th{padding:.6rem;text-align:left;border-bottom:1px solid #eee}@media(max-width:700px){main{grid-template-columns:repeat(2,1fr)}}</style><h1>EV Charger Monitor</h1><main id=cards></main><section><h2>Chargers</h2><table><thead><tr><th>ID</th><th>Status</th><th>Power</th><th>Energy</th><th>Last seen</th></tr></thead><tbody id=rows></tbody></table></section><script>async function refresh(){let c=await (await fetch('/api/chargers')).json();let s=await (await fetch('/api/sessions')).json();let m=await (await fetch('/api/export/meter-values')).json();cards.innerHTML=[['Total Chargers',c.length],['Online Chargers',c.filter(x=>x.last_seen&&Date.now()-Date.parse(x.last_seen)<180000).length],['Active Sessions',s.filter(x=>!x.end_time).length],['Energy Readings',m.length]].map(x=>`<div class=card><small>${x[0]}</small><h2>${x[1]}</h2></div>`).join('');rows.innerHTML=c.map(x=>{let v=m.filter(y=>y.charger_id==x.charger_id).pop()||{};return `<tr><td>${x.charger_id}</td><td>${x.status||'Unknown'}</td><td>${v.power??'-'}</td><td>${v.energy??'-'}</td><td>${x.last_seen||'-'}</td></tr>`}).join('')}refresh();setInterval(refresh,10000)</script>"""

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT",8000)))
