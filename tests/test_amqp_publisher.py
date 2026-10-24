import json
from pathlib import Path

import pika.exceptions
from pika.exceptions import NackError, UnroutableError
import pytest

from edge_gateway import create_app
from edge_gateway.config import Config
from edge_gateway.db import database
from edge_gateway.models import Outbox
from edge_gateway.services.amqp import AmqpEventPublisher, to_envelope
from edge_gateway.services.transport import CloudRejected, CloudUnavailable

PLATFORM_CONTRACTS = (
    Path(__file__).resolve().parent.parent.parent
    / "edifika"
    / "Edifika-IoT-Platform"
    / "contracts"
    / "schemas"
)


class FakeChannel:
    def __init__(self):
        self.published = []
        self.confirm_enabled = False
        self.fail_on = {}

    def confirm_delivery(self):
        self.confirm_enabled = True

    def basic_publish(self, exchange, routing_key, body, properties, mandatory=True):
        data = json.loads(body)
        eid = data.get("eventId")
        if eid in self.fail_on:
            raise self.fail_on[eid]
        self.published.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
                "mandatory": mandatory,
            }
        )

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.channel_obj = FakeChannel()
        self.is_closed = False
        self.is_open = True

    def channel(self):
        return self.channel_obj

    def close(self):
        self.is_closed = True
        self.is_open = False


class FlakyFactory:
    def __init__(self, fail_times, conn):
        self.fail_times = fail_times
        self.conn = conn
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise pika.exceptions.AMQPConnectionError("connection failed")
        return self.conn


def cfg():
    return Config(
        service_token="t",
        gateway_id="edge-gateway-01",
        amqp_url="amqp://guest:guest@localhost:5672/",
        amqp_events_exchange="edifika.events",
    )


def ev(eid, kind="reading"):
    return {
        "eventId": eid,
        "kind": kind,
        "occurredAt": "2026-10-23T15:00:00Z",
        "payload": {
            "deviceId": "esp32-acc-01",
            "sensor": "temperature",
            "value": 20.0,
            "unit": "°C",
        },
    }


@pytest.fixture
def fake_conn():
    return FakeConnection()


@pytest.fixture
def app_with_events_fake(config, clock):
    fake_conn = FakeConnection()
    fake_conn.channel_obj.fail_on = {"ev-2": NackError([])}
    publisher = AmqpEventPublisher(config, connection_factory=lambda: fake_conn)
    app = create_app(config, clock=clock)
    gw = app.extensions["gateway"]
    gw.forwarder.cloud = publisher
    with database.connection_context():
        gw.outbox.enqueue("reading", {"deviceId": "esp32-acc-01", "val": 1})
        gw.outbox.enqueue("reading", {"deviceId": "esp32-acc-01", "val": 2})
        gw.outbox.enqueue("reading", {"deviceId": "esp32-acc-01", "val": 3})
        rows = list(Outbox.select().order_by(Outbox.id))
        rows[0].event_id = "ev-1"
        rows[0].save()
        rows[1].event_id = "ev-2"
        rows[1].save()
        rows[2].event_id = "ev-3"
        rows[2].save()
        yield gw


def test_envelope_follows_contract():
    rk, body, props = to_envelope(
        {
            "eventId": "a" * 32,
            "kind": "command_result",
            "occurredAt": "2026-10-23T15:00:00Z",
            "payload": {
                "deviceId": "esp32-acc-01",
                "commandId": "c1",
                "type": "rgb",
                "status": "OK",
                "detail": None,
            },
        },
        "edge-gateway-01",
    )
    assert rk == "edge.command-result"
    assert json.loads(body) == {
        "eventId": "a" * 32,
        "eventType": "CommandResult",
        "eventVersion": 1,
        "occurredAt": "2026-10-23T15:00:00Z",
        "producer": "edge-gateway",
        "buildingId": None,
        "gatewayCode": "edge-gateway-01",
        "correlationId": "c1",
        "payload": {
            "deviceId": "esp32-acc-01",
            "commandId": "c1",
            "type": "rgb",
            "status": "OK",
            "detail": None,
        },
    }
    assert (
        props["message_id"] == "a" * 32
        and props["type"] == "CommandResult"
        and props["delivery_mode"] == 2
        and props["correlation_id"] == "c1"
    )


@pytest.mark.skipif(
    not (PLATFORM_CONTRACTS / "envelope.schema.json").exists(),
    reason="Edifika-IoT-Platform contracts not present",
)
@pytest.mark.parametrize(
    "kind",
    [
        "access_attempt",
        "reading",
        "alert",
        "device_status",
        "command_result",
        "credential_changed",
    ],
)
def test_envelope_validates_against_platform_schema(kind):
    import jsonschema

    env_schema = json.loads(
        (PLATFORM_CONTRACTS / "envelope.schema.json").read_text(encoding="utf-8")
    )
    payloads = {
        "access_attempt": {
            "deviceId": "esp32-acc-01",
            "areaId": None,
            "credential": "04A1B2C3",
            "credentialType": "RFID",
            "residentId": "15",
            "result": "GRANTED",
            "reason": "ok",
            "accessMode": "FREE_ACCESS",
        },
        "reading": {
            "deviceId": "esp32-acc-01",
            "areaId": None,
            "sensor": "temperature",
            "value": 24.5,
            "unit": "°C",
            "capturedAt": "2026-10-23T15:04:00Z",
            "timeSuspect": False,
        },
        "alert": {
            "alert": "sensor_fault_temperature",
            "state": "raised",
            "deviceId": "esp32-acc-01",
            "sensor": "temperature",
        },
        "device_status": {"deviceId": "esp32-acc-01", "event": "online"},
        "command_result": {
            "deviceId": "esp32-acc-01",
            "commandId": "c1",
            "type": "rgb",
            "status": "OK",
            "detail": None,
        },
        "credential_changed": {
            "uid": "04A1B2C3",
            "residentId": "guardia-1",
            "status": "ACTIVE",
            "source": "edge_manual",
        },
    }
    rk, body, props = to_envelope(
        {
            "eventId": "a" * 32,
            "kind": kind,
            "occurredAt": "2026-10-23T15:00:00Z",
            "payload": payloads[kind],
        },
        "edge-gateway-01",
    )
    envelope_data = json.loads(body)
    jsonschema.validate(envelope_data, env_schema)

    event_type = "".join(part.capitalize() for part in kind.split("_"))
    payload_schema_file = PLATFORM_CONTRACTS / "payloads" / f"{event_type}.schema.json"
    if payload_schema_file.exists():
        payload_schema = json.loads(payload_schema_file.read_text(encoding="utf-8"))
        jsonschema.validate(envelope_data["payload"], payload_schema)


def test_publishes_with_confirms_and_mandatory(fake_conn):
    assert AmqpEventPublisher(cfg(), connection_factory=lambda: fake_conn).post_events(
        [ev("1"), ev("2")]
    ) == ["1", "2"]
    ch = fake_conn.channel_obj
    assert ch.confirm_enabled and all(
        p["mandatory"] and p["exchange"] == "edifika.events" for p in ch.published
    )


def test_first_event_unroutable_is_retryable(fake_conn):
    fake_conn.channel_obj.fail_on = {"1": UnroutableError([])}
    with pytest.raises(CloudUnavailable):
        AmqpEventPublisher(cfg(), connection_factory=lambda: fake_conn).post_events(
            [ev("1"), ev("2")]
        )


def test_later_failure_returns_confirmed_prefix(fake_conn):
    fake_conn.channel_obj.fail_on = {"2": NackError([])}
    assert AmqpEventPublisher(cfg(), connection_factory=lambda: fake_conn).post_events(
        [ev("1"), ev("2"), ev("3")]
    ) == ["1"]


def test_connection_error_is_unavailable_then_reconnects(fake_conn):
    factory = FlakyFactory(fail_times=1, conn=fake_conn)
    pub = AmqpEventPublisher(cfg(), connection_factory=factory)
    with pytest.raises(CloudUnavailable):
        pub.post_events([ev("1")])
    assert pub.post_events([ev("1")]) == ["1"] and factory.calls == 2


def test_forwarder_marks_only_confirmed_prefix_as_sent(app_with_events_fake):
    gw = app_with_events_fake
    delivered = gw.forwarder.flush()
    assert delivered == 1
    rows = list(Outbox.select().order_by(Outbox.id))
    assert rows[0].sent_at is not None
    assert rows[1].sent_at is None
    assert rows[2].sent_at is None


def test_serialization_error_raises_cloud_rejected(fake_conn):
    pub = AmqpEventPublisher(cfg(), connection_factory=lambda: fake_conn)
    bad_event = {
        "eventId": "1",
        "kind": "bad",
        "occurredAt": "2026-10-23T15:00:00Z",
        "payload": {"unserializable": object()},
    }
    with pytest.raises(CloudRejected):
        pub.post_events([bad_event])

