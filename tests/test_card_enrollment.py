"""Card enrollment mode: the cloud puts a reader in enrollment mode, the node reports the
next card on the `enrollment` channel and the gateway forwards it as `card_enrolled`
without deciding any access."""
import json

import pytest

from edge_gateway.models import AccessAttempt, Outbox
from edge_gateway.mqtt import contract
from edge_gateway.services.cloud_commands import CloudCommandHandler

from .helpers import msg

PREFIX = "edifika/v1"
UID = "04A1B2C3"


def events(kind=None):
    rows = [(e.kind, json.loads(e.payload)) for e in Outbox.select().order_by(Outbox.id)]
    return [p for k, p in rows if kind is None or k == kind]


def enroll_order(command_id="cmd-enroll-1", device_code="door-1", params=None):
    return {
        "eventId": f"evt-{command_id}",
        "eventType": "DeviceCommandRequested",
        "eventVersion": 1,
        "occurredAt": "2026-11-02T06:00:00Z",
        "producer": "iot-access-management",
        "payload": {
            "commandId": command_id,
            "deviceCode": device_code,
            "type": "enroll",
            "params": {"active": True, "seconds": 60} if params is None else params,
            "expiresAt": "2099-01-01T00:00:00Z",
        },
    }


@pytest.fixture
def door(gw):
    device = gw.devices.register("door-1", area_id="pool", capabilities=["rfid", "lock", "oled", "buzzer"])
    gw.devices.touch(device)
    Outbox.delete().execute()
    return device


@pytest.fixture
def handler(gw):
    return CloudCommandHandler(gw.config, gw.clock, gw.devices, gw.access, gw.commands, gw.sync, gw.outbox)


# --- MQTT contract --------------------------------------------------------------------------
def test_the_enrollment_channel_is_part_of_the_contract():
    assert f"{PREFIX}/nodes/+/enrollment" in contract.subscription_topics(PREFIX)
    topic, raw = msg(PREFIX, "door-1", "enrollment", credentialType="RFID", credential="04 a1 b2 c3")
    device_id, channel, data = contract.parse_message(PREFIX, topic, raw)
    assert (device_id, channel, data["credential"]) == ("door-1", "enrollment", "04 a1 b2 c3")


def test_an_enrollment_message_without_credential_is_rejected():
    topic, raw = msg(PREFIX, "door-1", "enrollment", credentialType="RFID")
    with pytest.raises(contract.ContractError):
        contract.parse_message(PREFIX, topic, raw)


# --- node -> gateway -> cloud ---------------------------------------------------------------
def test_a_card_read_in_enrollment_mode_is_forwarded_and_not_evaluated(gw, door, publisher):
    # The firmware sends the compact UID; the gateway upper-cases it like access reads
    # (the cloud fully normalises it again when it is captured).
    topic, raw = msg(PREFIX, "door-1", "enrollment", credentialType="RFID", credential="04a1b2c3")

    assert gw.router.handle(topic, raw) is True

    [event] = events("card_enrolled")
    assert event == {"deviceId": "door-1", "deviceCode": "door-1", "uid": UID, "credentialType": "RFID"}
    assert AccessAttempt.select().count() == 0, "an enrollment read is not an access attempt"
    assert events("access_attempt") == []
    assert publisher.commands("door-1", "access_result") == [], "the door is never opened"


def test_an_enrollment_read_from_an_unknown_node_is_dropped(gw, door):
    topic, raw = msg(PREFIX, "ghost-1", "enrollment", credentialType="RFID", credential=UID)
    gw.router.handle(topic, raw)
    assert events("card_enrolled") == []


# --- cloud -> gateway -> node ---------------------------------------------------------------
def test_the_enroll_command_is_forwarded_to_the_node(handler, door, publisher):
    handler.handle(enroll_order())

    [sent] = publisher.commands("door-1", "enroll")
    assert sent["params"] == {"active": True, "seconds": 60}
    assert sent["commandId"] == "cmd-enroll-1"


def test_leaving_enrollment_mode_is_always_forwarded(handler, door, publisher):
    door.status = "OFFLINE"
    door.save()

    handler.handle(enroll_order(params={"active": False}))

    [sent] = publisher.commands("door-1", "enroll")
    assert sent["params"] == {"active": False}


def _last_result():
    return events("command_result")[-1]


def test_enroll_on_an_offline_reader_fails(handler, door, publisher):
    door.status = "OFFLINE"
    door.save()

    handler.handle(enroll_order())

    assert publisher.commands("door-1", "enroll") == []
    assert (_last_result()["status"], _last_result()["detail"]) == ("FAILED", "device unavailable")


@pytest.mark.parametrize("params", [{}, {"active": "yes"}, {"active": True, "seconds": 0}, {"active": True, "seconds": 9999}])
def test_enroll_with_invalid_params_fails(handler, door, publisher, params):
    handler.handle(enroll_order(params=params))

    assert publisher.commands("door-1", "enroll") == []
    assert _last_result()["status"] == "FAILED"
