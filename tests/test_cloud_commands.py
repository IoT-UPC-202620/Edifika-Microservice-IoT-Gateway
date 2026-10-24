from datetime import timedelta
import json
import pytest

from edge_gateway.config import Config
from edge_gateway.db import database
from edge_gateway.models import Device, DoorReservation, Outbox
from edge_gateway.services.amqp import AmqpCommandConsumer
from edge_gateway.services.cloud_commands import CloudCommandHandler
from tests.helpers import FakeCloud, msg

PREFIX = "edifika/v1"


def order(
    command_id="cmd-1",
    device_code="esp32-acc-01",
    type_="rgb",
    color="#00ff88",
    expires_at=None,
):
    return {
        "eventId": "evt-1",
        "eventType": "DeviceCommandRequested",
        "eventVersion": 1,
        "occurredAt": "2026-10-23T15:00:00Z",
        "producer": "iot-access-management",
        "payload": {
            "commandId": command_id,
            "deviceCode": device_code,
            "type": type_,
            "params": {"color": color} if color is not None else {},
            "expiresAt": expires_at or "2099-01-01T00:00:00Z",
        },
    }


def sync_requested(sync_version=43):
    return {
        "eventId": "evt-sync-1",
        "eventType": "SyncRequested",
        "eventVersion": 1,
        "occurredAt": "2026-10-23T15:00:00Z",
        "producer": "iot-access-management",
        "payload": {"syncVersion": sync_version},
    }


def last_command_result():
    rows = list(
        Outbox.select().where(Outbox.kind == "command_result").order_by(Outbox.id.desc())
    )
    return json.loads(rows[0].payload) if rows else None


@pytest.fixture
def reserved_door(gw, clock):
    now = clock.now()
    d = Device.create(
        device_id="esp32-acc-01",
        name="Puerta piscina",
        location="Piso 1",
        capabilities='["lock", "rfid", "oled", "buzzer", "rgb_led"]',
        status="ACTIVE",
        managed="cloud",
        current_mode="RESERVED",
    )
    DoorReservation.create(
        source_id="res-1",
        device_id="esp32-acc-01",
        holder_resident_id="15",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=55),
    )
    return d


@pytest.fixture
def handler(gw):
    return CloudCommandHandler(
        gw.config,
        gw.clock,
        gw.devices,
        gw.access,
        gw.commands,
        gw.sync,
        gw.outbox,
    )


@pytest.fixture
def cgw(app):
    with database.connection_context():
        gw = app.extensions["gateway"]
        fake = FakeCloud()
        gw.sync.cloud = fake
        gw.forwarder.cloud = fake
        yield gw


class ConsumerFakeChannel:
    def __init__(self):
        self.declared_queues = []
        self.bindings = []
        self.qos = None
        self.callbacks = []
        self.acks = []
        self.nacks = []

    def queue_declare(self, queue, durable=True, arguments=None):
        self.declared_queues.append(
            {"queue": queue, "durable": durable, "arguments": arguments}
        )

    def queue_bind(self, queue, exchange, routing_key):
        self.bindings.append(
            {"queue": queue, "exchange": exchange, "routing_key": routing_key}
        )

    def basic_qos(self, prefetch_count):
        self.qos = prefetch_count

    def basic_consume(self, queue, on_message_callback, auto_ack=False):
        self.callbacks.append(on_message_callback)

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue=False):
        self.nacks.append({"delivery_tag": delivery_tag, "requeue": requeue})

    def start_consuming(self):
        pass

    def stop_consuming(self):
        pass

    def close(self):
        pass


class ConsumerFakeConnection:
    def __init__(self):
        self.channel_obj = ConsumerFakeChannel()
        self.is_closed = False

    def channel(self):
        return self.channel_obj

    def close(self):
        self.is_closed = True


def test_rgb_on_reserved_door_uses_cloud_command_id(
    gw, publisher, reserved_door, handler
):
    handler.handle(order("cmd-1", color="#00ff88"))
    c = publisher.commands("esp32-acc-01", "rgb")[-1]
    assert (c["commandId"], c["params"]) == ("cmd-1", {"color": "#00FF88"})


def test_ack_ok_reports_result_ok(gw, publisher, reserved_door, handler):
    handler.handle(order("cmd-1", color="#00ff88"))
    gw.router.handle(*msg(PREFIX, "esp32-acc-01", "ack", commandId="cmd-1", status="OK"))
    result = last_command_result()
    assert result["status"] == "OK"
    assert result["commandId"] == "cmd-1"


def test_duplicate_order_is_sent_once(gw, publisher, reserved_door, handler):
    handler.handle(order("cmd-1", color="#00ff88"))
    handler.handle(order("cmd-1", color="#00ff88"))
    assert len(publisher.commands("esp32-acc-01", "rgb")) == 1


def test_rgb_on_free_access_door_fails(gw, publisher, handler):
    Device.create(
        device_id="esp32-acc-01",
        capabilities='["lock", "rgb_led"]',
        status="ACTIVE",
        managed="cloud",
        current_mode="FREE_ACCESS",
    )
    handler.handle(order("cmd-1", color="#00ff88"))
    assert len(publisher.commands("esp32-acc-01", "rgb")) == 0
    result = last_command_result()
    assert result["status"] == "FAILED"
    assert result["detail"] == "door is not reserved"


def test_expired_order_reports_expired(
    gw, publisher, clock, reserved_door, handler
):
    now_iso = (clock.now() - timedelta(seconds=1)).isoformat() + "Z"
    handler.handle(order("cmd-exp", expires_at=now_iso))
    assert len(publisher.commands("esp32-acc-01", "rgb")) == 0
    result = last_command_result()
    assert result["status"] == "EXPIRED"


def test_unknown_device_fails(gw, handler):
    handler.handle(order("cmd-unk", device_code="unknown-dev"))
    result = last_command_result()
    assert result["status"] == "FAILED"
    assert result["detail"] == "unknown device"


def test_invalid_color_fails(gw, reserved_door, handler):
    handler.handle(order("cmd-bad-color", color="not-a-color"))
    result = last_command_result()
    assert result["status"] == "FAILED"
    assert result["detail"] == "invalid color"


def test_offline_device_fails(gw, reserved_door, handler):
    reserved_door.status = "OFFLINE"
    reserved_door.save()
    handler.handle(order("cmd-off", color="#00ff88"))
    result = last_command_result()
    assert result["status"] == "FAILED"
    assert result["detail"] == "device unavailable"


def test_sync_requested_triggers_pull(cgw):
    handler = CloudCommandHandler(
        cgw.config,
        cgw.clock,
        cgw.devices,
        cgw.access,
        cgw.commands,
        cgw.sync,
        cgw.outbox,
    )
    cgw.sync.cloud.snapshot = {"version": 43, "full": False}
    initial = len(cgw.sync.cloud.sync_requests)
    handler.handle(sync_requested(43))
    assert len(cgw.sync.cloud.sync_requests) == initial + 1


def test_consumer_declares_queue_binding_and_dlx():
    conn = ConsumerFakeConnection()
    cfg = Config(
        service_token="t",
        gateway_id="gw-1",
        amqp_commands_exchange="edifika.edge.commands",
        amqp_prefetch=5,
    )
    consumer = AmqpCommandConsumer(cfg, None, connection_factory=lambda: conn)
    consumer._setup(conn.channel_obj)

    ch = conn.channel_obj
    assert len(ch.declared_queues) == 1
    q = ch.declared_queues[0]
    assert q["queue"] == "edge.gw-1.commands"
    assert q["durable"] is True
    assert q["arguments"] == {
        "x-dead-letter-exchange": "edifika.dlx",
        "x-dead-letter-routing-key": "edge.gw-1.commands",
    }
    assert ch.bindings == [
        {
            "queue": "edge.gw-1.commands",
            "exchange": "edifika.edge.commands",
            "routing_key": "gw-1.#",
        }
    ]
    assert ch.qos == 5


def test_consumer_nacks_without_requeue_on_error():
    conn = ConsumerFakeConnection()
    cfg = Config(service_token="t", gateway_id="gw-1")

    class FailingHandler:
        def handle(self, envelope):
            raise RuntimeError("bad payload")

    consumer = AmqpCommandConsumer(
        cfg, FailingHandler(), connection_factory=lambda: conn
    )
    ch = conn.channel_obj
    consumer._setup(ch)
    cb = ch.callbacks[0]

    method = type("Method", (), {"delivery_tag": 42})()
    cb(ch, method, None, b"not valid json")
    assert ch.nacks == [{"delivery_tag": 42, "requeue": False}]
