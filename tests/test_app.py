import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import Charger, FastAPIWebSocketAdapter, _websocket_handler, app, health, init_db
from ocpp.v16 import call_result


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def receive_text(self):
        return '[2,"1","Heartbeat",{}]'

    async def send_text(self, message):
        self.sent.append(message)


class AppTests(unittest.TestCase):
    def setUp(self):
        # Keep test telemetry separate from the local/Railway database.
        # Use the workspace rather than the system temp directory: the test
        # runner may be sandboxed from accessing the latter.
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.db_patch = patch.object(app_module, "DATA_DIR", self.temp_dir.name)
        self.path_patch = patch.object(
            app_module, "DB_PATH", str(Path(self.temp_dir.name) / "ev_monitor.db")
        )
        self.db_patch.start()
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_application_imports_in_a_clean_process(self):
        """Catch missing/incompatible imports before the deployment starts."""
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_health(self):
        self.assertEqual(health(), {"status": "ok"})

    def test_database_initializes(self):
        init_db()  # Must be safe on an existing Railway/local database.

    def test_installed_ocpp_response_classes(self):
        self.assertEqual(call_result.BootNotification.__name__, "BootNotification")
        self.assertEqual(call_result.Heartbeat.__name__, "Heartbeat")
        self.assertEqual(call_result.StatusNotification.__name__, "StatusNotification")

    def test_boot_notification_frame(self):
        frame = [2, "boot001", "BootNotification", {
            "chargePointVendor": "Autel",
            "chargePointModel": "MaxiCharger",
        }]
        message_type, unique_id, action, payload = json.loads(json.dumps(frame))
        self.assertEqual((message_type, unique_id, action), (2, "boot001", "BootNotification"))

        async def run():
            init_db()
            charger = Charger("CP001", FakeWebSocket())
            result = await charger.boot_notification(
                charge_point_vendor=payload["chargePointVendor"],
                charge_point_model=payload["chargePointModel"],
            )
            self.assertEqual(result.status, "Accepted")
            self.assertEqual(result.interval, 60)

        asyncio.run(run())

    def test_all_supported_ocpp_handlers(self):
        async def run():
            init_db()
            charger = Charger("CP001", FakeWebSocket())

            heartbeat = await charger.heartbeat()
            self.assertEqual(heartbeat.current_time, heartbeat.current_time)

            status = await charger.status_notification(
                connector_id=1,
                error_code="NoError",
                status="Available",
            )
            self.assertIsInstance(status, call_result.StatusNotification)

            meter = await charger.meter_values(
                connector_id=1,
                meter_value=[{
                    "timestamp": "2026-09-10T07:00:00Z",
                    "sampledValue": [
                        {"measurand": "Power.Active.Import", "value": "2300", "unit": "W"},
                        {"measurand": "Voltage", "value": "230", "unit": "V"},
                        {"measurand": "Current.Import", "value": "10", "unit": "A"},
                        {"measurand": "Energy.Active.Import.Register", "value": "12500", "unit": "Wh"},
                        {"measurand": "Frequency", "value": "50", "unit": "Hz"},
                        {"measurand": "Power.Factor", "value": "0.99"},
                    ],
                }],
            )
            self.assertIsInstance(meter, call_result.MeterValues)

            started = await charger.start_transaction(
                connector_id=1,
                id_tag="TEST-TAG",
                meter_start=12500,
                timestamp="2026-09-10T07:00:00Z",
            )
            self.assertEqual(started.transaction_id, 0)
            self.assertEqual(started.id_tag_info.status, "Accepted")

            stopped = await charger.stop_transaction(
                transaction_id=0,
                meter_stop=12600,
                timestamp="2026-09-10T07:30:00Z",
            )
            self.assertEqual(stopped.id_tag_info.status, "Accepted")

            configuration = await charger.get_configuration(
                key=["NumberOfConnectors", "NotAConfigurationKey"]
            )
            self.assertEqual(configuration.configuration_key[0].key, "NumberOfConnectors")
            self.assertTrue(configuration.configuration_key[0].readonly)
            self.assertEqual(configuration.unknown_key, ["NotAConfigurationKey"])

        asyncio.run(run())

    def test_websocket_adapter(self):
        async def run():
            raw = FakeWebSocket()
            adapter = FastAPIWebSocketAdapter(raw)
            self.assertEqual(await adapter.recv(), '[2,"1","Heartbeat",{}]')
            await adapter.send('reply')
            self.assertEqual(raw.sent, ['reply'])

        asyncio.run(run())

    def test_connection_starts_ocpp_loop_without_trigger_message(self):
        class FakeConnectionWebSocket:
            headers = {"sec-websocket-protocol": "ocpp1.6"}

            async def accept(self, subprotocol=None):
                self.subprotocol = subprotocol

        class FakeCharger:
            instances = []

            def __init__(self, charger_id, connection):
                self.charger_id = charger_id
                self.connection = connection
                self.calls = []
                self.__class__.instances.append(self)

            async def call(self, payload):
                self.calls.append(payload)
                return "Accepted"

            async def start(self):
                self.started = True
                raise app_module.WebSocketDisconnect()

        async def run():
            websocket = FakeConnectionWebSocket()
            with patch.object(app_module, "Charger", FakeCharger):
                await _websocket_handler(websocket, "CP001")
            self.assertEqual(len(FakeCharger.instances), 1)
            self.assertTrue(FakeCharger.instances[0].started)
            self.assertEqual(FakeCharger.instances[0].calls, [])

        asyncio.run(run())

    def test_required_routes_exist(self):
        paths = {route.path for route in app.routes}
        self.assertIn("/health", paths)
        self.assertIn("/{charger_id}", paths)
        self.assertIn("/ws/webSocket", paths)


if __name__ == "__main__":
    unittest.main()
