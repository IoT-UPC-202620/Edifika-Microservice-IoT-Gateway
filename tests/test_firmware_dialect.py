import json
from datetime import timedelta

import pytest

from edge_gateway.clock import to_epoch
from edge_gateway.models import Device, DoorReservation, Outbox
from edge_gateway.mqtt import contract

from .helpers import firmware_msg, msg


def test_firmware_heartbeat_with_string_version_and_iso_ts_is_accepted(gw):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01")
    topic, raw = firmware_msg(P, "esp32-acc-01", "heartbeat", deviceId="esp32-acc-01", fw="v0.3.0")
    assert gw.router.handle(topic, raw) is True
    assert Device.get_by_id("esp32-acc-01").firmware == "v0.3.0"
    assert not gw.router.rejected


def test_access_without_device_id_uses_topic(gw, publisher):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01", capabilities=["lock"])
    topic, raw = firmware_msg(
        P, "esp32-acc-01", "access", credentialType="RFID", credential="04A1B2C3"
    )
    assert gw.router.handle(topic, raw) is True
    assert publisher.commands("esp32-acc-01", "access_result")


def test_null_ts_reading_is_kept_as_time_suspect_and_no_time_sync(gw, publisher):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01", capabilities=["temperature"])
    topic, raw = firmware_msg(
        P, "esp32-acc-01", "readings", ts=None, sensor="temperature", value=24.0, unit="C"
    )
    assert gw.router.handle(topic, raw) is True
    p = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "reading"][-1]
    assert (p["unit"], p["timeSuspect"]) == ("°C", True)
    assert not publisher.commands("esp32-acc-01", "time_sync")


def test_ack_error_is_failed_and_empty_detail_is_none(gw, publisher):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01", capabilities=["lock"])
    cmd = gw.commands.send("esp32-acc-01", "unlock")
    topic, raw = firmware_msg(
        P, "esp32-acc-01", "ack", commandId=cmd.command_id, status="ERROR", detail=""
    )
    assert gw.router.handle(topic, raw) is True
    p = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "command_result"][-1]
    assert (p["status"], p["detail"]) == ("FAILED", None)


def test_mismatched_device_id_is_still_rejected(gw):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01")
    topic, raw = firmware_msg(
        P, "esp32-acc-01", "access", deviceId="other-id", credential="04A1"
    )
    assert gw.router.handle(topic, raw) is False
    assert gw.router.rejected.get("device_mismatch", 0) >= 1


def test_node_back_online_receives_current_mode(gw, publisher, clock):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01", capabilities=["lock"], location="door")
    DoorReservation.create(
        source_id="r1",
        device_id="esp32-acc-01",
        holder_resident_id="15",
        starts_at=clock.now() - timedelta(minutes=5),
        ends_at=clock.now() + timedelta(minutes=30),
    )
    # first heartbeat activates it
    gw.devices.on_heartbeat("esp32-acc-01", {"ts": to_epoch(clock.now())})
    # reserved door goes OFFLINE
    clock.advance(seconds=60)
    gw.devices.check_offline()
    assert Device.get_by_id("esp32-acc-01").status == "OFFLINE"
    # then heartbeat brings it back online
    clock.advance(seconds=1)
    topic, raw = firmware_msg(P, "esp32-acc-01", "heartbeat", deviceId="esp32-acc-01")
    assert gw.router.handle(topic, raw) is True
    assert publisher.commands("esp32-acc-01", "mode")[-1]["params"]["mode"] == "RESERVED"


def test_v010_integer_envelope_still_accepted(gw, publisher):
    P = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-old", capabilities=["humidity"])
    topic, raw = msg(P, "esp32-old", "readings", sensor="humidity", value=50.0, unit="%")
    assert gw.router.handle(topic, raw) is True
    p = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "reading"][-1]
    assert p["value"] == 50.0
