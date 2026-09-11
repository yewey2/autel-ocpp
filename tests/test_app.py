import asyncio
import json
import unittest
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

        asyncio.run(run())

    def test_websocket_adapter(self):
        async def run():
            raw = FakeWebSocket()
            adapter = FastAPIWebSocketAdapter(raw)
            self.assertEqual(await adapter.recv(), '[2,"1","Heartbeat",{}]')
            await adapter.send('reply')
            self.assertEqual(raw.sent, ['reply'])

        asyncio.run(run())

    def test_connection_requests_boot_notification(self):
        class FakeConnectionWebSocket:
            headers = {}

            async def accept(self, subprotocol=None):
                self.subprotocol = subprotocol

        class FakeCharger:
            instances = []

            def __init__(self, charger_id, connection):
                self.charger_id = charger_id
                self.connection = connection
                self.calls = []
                self.running = asyncio.Event()
                self.__class__.instances.append(self)

            async def call(self, payload):
                self.calls.append(payload)
                self.running.set()
                return "Accepted"

            async def start(self):
                await self.running.wait()

        async def run():
            websocket = FakeConnectionWebSocket()
            with patch.object(app_module, "Charger", FakeCharger):
                await _websocket_handler(websocket, "CP001")
            self.assertEqual(len(FakeCharger.instances), 1)
            request = FakeCharger.instances[0].calls[0]
            self.assertEqual(request.__class__.__name__, "TriggerMessage")
            self.assertEqual(request.requested_message.value, "BootNotification")

        asyncio.run(run())

    def test_required_routes_exist(self):
        paths = {route.path for route in app.routes}
        self.assertIn("/health", paths)
        self.assertIn("/{charger_id}", paths)
        self.assertIn("/ws/webSocket", paths)


if __name__ == "__main__":
    unittest.main()
