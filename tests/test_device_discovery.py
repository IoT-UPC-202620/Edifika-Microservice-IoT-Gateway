"""Device discovery: a node announces itself, the gateway identifies it and keeps it pending
(and inert) until the cloud adopts it; only `identify` reaches a pending node."""
import json

import pytest

from edge_gateway.models import AccessAttempt, ActiveAlert, DiscoveredDevice, Outbox
from edge_gateway.mqtt import contract
from edge_gateway.services.cloud_commands import CloudCommandHandler
from edge_gateway.services.devices import DISCOVERY_REPORT_INTERVAL_S

from .helpers import msg

PREFIX = "edifika/v1"
NODE = "esp32-3c71bf"
CAPS = ["RFID", "LOCK", "BUZZER", "OLED", "RGB_LED", "TEMPERATURE", "HUMIDITY"]


def events(kind=None):
    rows = [(e.kind, json.loads(e.payload)) for e in Outbox.select().order_by(Outbox.id)]
    return [p for k, p in rows if kind is None or k == kind]


def announce(gw, device_id=NODE, **overrides):
    fields = {
        "mac": "24:6f:28:3c:71:bf",
        "fw": "v0.6.0",
        "model": "door-node",
        "capabilities": CAPS,
        "ip": "192.168.1.40",
        "rssi": -58,
        "pairingCode": "4821",
        **overrides,
    }
    topic, raw = msg(PREFIX, device_id, "announce", **fields)
    return gw.router.handle(topic, raw)


def order(command_id="cmd-identify-1", device_code=NODE, params=None, type_="identify"):
    return {
        "eventId": f"evt-{command_id}",
        "eventType": "DeviceCommandRequested",
        "eventVersion": 1,
        "occurredAt": "2026-11-03T15:00:00Z",
        "producer": "iot-access-management",
        "payload": {
            "commandId": command_id,
            "deviceCode": device_code,
            "type": type_,
            "params": {} if params is None else params,
            "expiresAt": "2099-01-01T00:00:00Z",
        },
    }


@pytest.fixture
def handler(gw):
    return CloudCommandHandler(gw.config, gw.clock, gw.devices, gw.access, gw.commands, gw.sync, gw.outbox)


# --- MQTT contract --------------------------------------------------------------------------
def test_the_announce_channel_is_part_of_the_contract():
    assert f"{PREFIX}/nodes/+/announce" in contract.subscription_topics(PREFIX)


def test_an_announce_without_pairing_code_is_rejected(gw):
    topic, raw = msg(PREFIX, NODE, "announce", mac="24:6F:28:3C:71:BF", fw="v0.6.0", capabilities=CAPS)
    assert gw.router.handle(topic, raw) is False
    assert DiscoveredDevice.select().count() == 0


# --- identification ---------------------------------------------------------------------------
def test_an_unknown_node_is_kept_pending_and_reported_to_the_cloud(gw, publisher):
    assert announce(gw) is True

    pending = DiscoveredDevice.get(DiscoveredDevice.device_id == NODE)
    assert pending.mac == "24:6F:28:3C:71:BF"
    assert pending.pairing_code == "4821"
    [event] = events("device_discovered")
    assert event["deviceCode"] == NODE
    assert event["pairingCode"] == "4821"
    assert event["capabilities"] == sorted(CAPS)
    assert event["firstSeenAt"] == event["lastSeenAt"]
    # Inert: nothing is sent to a node that is not adopted
    assert publisher.sent == []


def test_repeated_announces_are_reported_again_only_when_something_changes_or_time_passes(gw, clock):
    announce(gw)
    clock.advance(seconds=60)
    announce(gw, rssi=-70)  # signal strength alone is not a change
    assert len(events("device_discovered")) == 1

    announce(gw, fw="v0.6.1")
    assert events("device_discovered")[-1]["firmware"] == "v0.6.1"

    clock.advance(seconds=DISCOVERY_REPORT_INTERVAL_S)
    announce(gw, fw="v0.6.1")
    assert len(events("device_discovered")) == 3
    assert DiscoveredDevice.get(DiscoveredDevice.device_id == NODE).last_seen == clock.now()


def test_a_pending_node_is_inert_and_does_not_raise_unregistered_alerts(gw, publisher):
    announce(gw)
    topic, raw = msg(PREFIX, NODE, "access", credentialType="RFID", credential="04A1B2C3")
    gw.router.handle(topic, raw)
    topic, raw = msg(PREFIX, NODE, "heartbeat", fw="v0.6.0")
    gw.router.handle(topic, raw)

    assert AccessAttempt.select().count() == 0
    assert publisher.sent == []
    assert ActiveAlert.select().where(ActiveAlert.device_id == NODE).count() == 0


def test_an_unknown_node_that_never_announced_still_raises_an_alert(gw):
    topic, raw = msg(PREFIX, "esp32-ffffff", "heartbeat", fw="v0.5.0")
    gw.router.handle(topic, raw)
    assert ActiveAlert.get(ActiveAlert.device_id == "esp32-ffffff").kind == "unregistered_device"


# --- adoption -------------------------------------------------------------------------------
def test_the_first_heartbeat_after_adoption_sends_mode_and_clears_the_pending_entry(gw, publisher):
    announce(gw)
    gw.devices.register(NODE, area_id="gym", capabilities=["rfid", "lock", "oled", "buzzer"])  # as the sync does

    topic, raw = msg(PREFIX, NODE, "heartbeat", fw="v0.6.0", mode="FREE_ACCESS")
    gw.router.handle(topic, raw)

    [mode] = publisher.commands(NODE, "mode")
    assert mode["params"] == {"mode": "FREE_ACCESS"}
    assert DiscoveredDevice.get_or_none(DiscoveredDevice.device_id == NODE) is None


def test_an_adopted_node_that_announces_again_gets_its_mode_back(gw, publisher):
    gw.devices.register(NODE, area_id="gym", capabilities=["rfid", "lock", "oled", "buzzer"])
    publisher.sent.clear()

    announce(gw)

    assert len(publisher.commands(NODE, "mode")) == 1
    assert DiscoveredDevice.select().count() == 0
    assert events("device_discovered") == []


# --- identify -------------------------------------------------------------------------------
def test_identify_reaches_a_pending_node(gw, handler, publisher):
    announce(gw)
    handler.handle(order(params={"seconds": 15}))

    [command] = publisher.commands(NODE, "identify")
    assert command["params"] == {"seconds": 15}
    assert command["commandId"] == "cmd-identify-1"


def test_identify_defaults_to_ten_seconds(gw, handler, publisher):
    announce(gw)
    handler.handle(order())
    assert publisher.commands(NODE, "identify")[0]["params"] == {"seconds": 10}


@pytest.mark.parametrize("seconds", [0, 61, "10", True])
def test_identify_rejects_invalid_seconds(gw, handler, publisher, seconds):
    announce(gw)
    handler.handle(order(params={"seconds": seconds}))
    assert publisher.commands(NODE, "identify") == []
    assert events("command_result")[-1]["detail"] == "invalid seconds"


def test_identify_for_an_unknown_node_fails(gw, handler, publisher):
    handler.handle(order(device_code="esp32-000000"))
    assert publisher.sent == []
    assert events("command_result")[-1]["detail"] == "unknown device"


def test_other_orders_for_a_pending_node_are_still_refused(gw, handler, publisher):
    announce(gw)
    handler.handle(order(command_id="cmd-led", type_="rgb", params={"color": "#00FF88"}))
    assert publisher.sent == []
    assert events("command_result")[-1]["detail"] == "unknown device"
