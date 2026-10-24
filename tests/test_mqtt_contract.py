from dataclasses import replace
import json

import pytest

from edge_gateway.config import Config
from edge_gateway.models import ActiveAlert, Outbox
from edge_gateway.mqtt import contract
from edge_gateway.mqtt.bridge import MqttBridge, PublishError
from edge_gateway.mqtt.router import MessageRouter

from .helpers import msg

PREFIX = "edifika/v1"


def test_topic_is_parsed_into_device_and_channel():
    assert contract.parse_topic(PREFIX, f"{PREFIX}/nodes/esp32-01/heartbeat") == ("esp32-01", "heartbeat")


@pytest.mark.parametrize(
    "topic",
    ["other/v1/nodes/d/heartbeat", f"{PREFIX}/nodes/d/unknown", f"{PREFIX}/nodes//heartbeat", f"{PREFIX}/nodes/d"],
)
def test_unexpected_topics_are_rejected(topic):
    with pytest.raises(contract.ContractError) as err:
        contract.parse_topic(PREFIX, topic)
    assert err.value.code == "unknown_topic"


def test_valid_reading_is_parsed():
    topic, raw = msg(PREFIX, "d1", "readings", sensor="humidity", value=41.5, unit="%")
    device_id, channel, data = contract.parse_message(PREFIX, topic, raw)
    assert (device_id, channel) == ("d1", "readings")
    assert data["value"] == 41.5 and data["sensor"] == "humidity"


def test_reading_without_measurement_is_accepted_as_null():
    topic, raw = msg(PREFIX, "d1", "readings", sensor="ultrasonic", value=None)
    assert contract.parse_message(PREFIX, topic, raw)[2]["value"] is None


def test_unknown_fields_are_ignored_so_firmware_can_add_data():
    topic, raw = msg(PREFIX, "d1", "heartbeat", rssi=-60)
    assert "rssi" not in contract.parse_message(PREFIX, topic, raw)[2]


@pytest.mark.parametrize(
    "raw, code",
    [
        ("not json", "invalid_json"),
        ("[1,2]", "invalid_payload"),
        (json.dumps({"v": 2, "deviceId": "d1", "ts": 1}), "unsupported_version"),
        (json.dumps({"v": 1, "deviceId": "d1"}), "invalid_payload"),
        (json.dumps({"v": 1, "deviceId": "other", "ts": 1}), "device_mismatch"),
    ],
)
def test_invalid_messages_are_rejected_with_a_reason(raw, code):
    with pytest.raises(contract.ContractError) as err:
        contract.parse_message(PREFIX, f"{PREFIX}/nodes/d1/heartbeat", raw)
    assert err.value.code == code
    assert err.value.device_id == "d1"


def test_access_defaults_to_rfid_and_rejects_unknown_types():
    topic, raw = msg(PREFIX, "d1", "access", credential="04A1")
    assert contract.parse_message(PREFIX, topic, raw)[2]["credentialType"] == "RFID"
    topic, raw = msg(PREFIX, "d1", "access", credential="04A1", credentialType="QR")
    with pytest.raises(contract.ContractError):
        contract.parse_message(PREFIX, topic, raw)


def test_router_dispatches_to_the_channel_handler(gw):
    seen = []
    router = MessageRouter(PREFIX)
    router.register("heartbeat", lambda device_id, data: seen.append((device_id, data["ts"])))
    topic, raw = msg(PREFIX, "d1", "heartbeat")
    assert router.handle(topic, raw) is True
    assert seen == [("d1", 1_780_000_000.0)]


def test_router_survives_a_bad_message_and_a_failing_handler(gw):
    router = MessageRouter(PREFIX)
    calls = []

    def flaky(device_id, data):
        calls.append(device_id)
        if len(calls) == 1:
            raise RuntimeError("boom")

    router.register("heartbeat", flaky)
    assert router.handle(f"{PREFIX}/nodes/d1/heartbeat", "garbage") is False
    topic, raw = msg(PREFIX, "d1", "heartbeat")
    assert router.handle(topic, raw) is False  # handler failed, router still alive
    assert router.handle(topic, raw) is True
    assert router.rejected["invalid_json"] == 1


def test_incompatible_firmware_is_reported_once(gw):
    topic = f"{PREFIX}/nodes/d1/heartbeat"
    raw = json.dumps({"v": 9, "deviceId": "d1", "ts": 1})
    for _ in range(3):
        gw.router.handle(topic, raw)
    events = [json.loads(e.payload) for e in Outbox.select()]
    assert [e["alert"] for e in events] == ["firmware_incompatible"]
    assert ActiveAlert.select().count() == 1


class StubClient:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self.tls_calls = []
        self.rc = 0

    def username_pw_set(self, *a): ...
    def reconnect_delay_set(self, **kw): ...
    def connect_async(self, *a, **kw): ...
    def loop_start(self): ...
    def loop_stop(self): ...
    def disconnect(self): ...
    def tls_set(self, **kw):
        self.tls_calls.append(kw)
    def tls_insecure_set(self, *a, **kw): ...

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        return type("Info", (), {"rc": self.rc})()


def router():
    return MessageRouter(PREFIX)


def test_bridge_enables_tls_with_ca_file(config):
    stub = StubClient()
    MqttBridge(replace(config, mqtt_enabled=True, mqtt_tls=True, mqtt_ca_file="ca.pem"), router(), client_factory=lambda: stub)
    assert stub.tls_calls == [{"ca_certs": "ca.pem"}]


def test_bridge_uses_system_cas_without_ca_file(config):
    stub = StubClient()
    MqttBridge(replace(config, mqtt_enabled=True, mqtt_tls=True), router(), client_factory=lambda: stub)
    assert stub.tls_calls == [{"ca_certs": None}]


def make_bridge(router=None):
    stub = StubClient()
    bridge = MqttBridge(Config(service_token="x"), router or MessageRouter(PREFIX), client_factory=lambda: stub)
    return bridge, stub


def test_bridge_subscribes_to_all_channels_on_connect():
    bridge, stub = make_bridge()
    bridge._on_connect(stub, None, None, 0)
    assert bridge.connected
    assert stub.subscribed == [f"{PREFIX}/nodes/+/{c}" for c in contract.CHANNELS]


def test_bridge_refuses_to_publish_while_disconnected():
    bridge, _ = make_bridge()
    with pytest.raises(PublishError):
        bridge.publish_command("d1", {"type": "unlock"})


def test_bridge_publishes_commands_on_the_device_topic():
    bridge, stub = make_bridge()
    bridge._on_connect(stub, None, None, 0)
    bridge.publish_command("d1", {"type": "unlock"})
    assert stub.published == [(f"{PREFIX}/nodes/d1/commands", {"type": "unlock"})]
    bridge._on_disconnect(stub, None, None, 0)
    assert bridge.probe()["ok"] is False


def test_bridge_never_raises_from_the_network_loop():
    class Boom(MessageRouter):
        def handle(self, topic, raw):
            raise RuntimeError("boom")

    bridge, _ = make_bridge(Boom(PREFIX))
    bridge._on_message(None, None, type("M", (), {"topic": "t", "payload": b"{}"})())
