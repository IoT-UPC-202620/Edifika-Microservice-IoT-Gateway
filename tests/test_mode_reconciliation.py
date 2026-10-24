"""A node reports its door mode and LED colour in every heartbeat; the gateway re-sends `mode`
whenever that differs from the latest configuration (reboot, reconnect, lost command)."""

from datetime import timedelta

import pytest

from edge_gateway.models import Device, DoorReservation

from .helpers import firmware_msg

DOOR = "esp32-acc-01"


@pytest.fixture
def door(gw, clock):
    gw.devices.register(DOOR, capabilities=["rfid", "lock", "rgb_led"], location="door")
    gw.router.handle(*firmware_msg(gw.config.mqtt_topic_prefix, DOOR, "heartbeat"))
    return Device.get_by_id(DOOR)


@pytest.fixture
def reserved_green(gw, clock, door):
    DoorReservation.create(
        source_id="r1",
        device_id=DOOR,
        holder_resident_id="15",
        starts_at=clock.now() - timedelta(minutes=1),
        ends_at=clock.now() + timedelta(minutes=5),
    )
    Device.update(led_color="#00FF00").where(Device.device_id == DOOR).execute()
    gw.access.check_mode_changes()
    return door


def heartbeat(gw, **state):
    return gw.router.handle(*firmware_msg(gw.config.mqtt_topic_prefix, DOOR, "heartbeat", **state))


def mode_commands(publisher):
    return publisher.commands(DOOR, "mode")


def test_rebooted_node_reporting_free_access_gets_reserved_mode_with_color(gw, publisher, clock, reserved_green):
    before = len(mode_commands(publisher))
    clock.advance(seconds=10)
    heartbeat(gw, mode="FREE_ACCESS", color=None)
    sent = mode_commands(publisher)[before:]
    assert [c["params"] for c in sent] == [{"mode": "RESERVED", "color": "#00FF00"}]


def test_node_in_sync_receives_nothing(gw, publisher, clock, reserved_green):
    before = len(mode_commands(publisher))
    clock.advance(seconds=10)
    heartbeat(gw, mode="RESERVED", color="#00ff00")
    assert len(mode_commands(publisher)) == before


def test_node_with_stale_color_gets_the_latest_color(gw, publisher, clock, reserved_green):
    before = len(mode_commands(publisher))
    clock.advance(seconds=10)
    heartbeat(gw, mode="RESERVED", color="#800080")
    sent = mode_commands(publisher)[before:]
    assert [c["params"] for c in sent] == [{"mode": "RESERVED", "color": "#00FF00"}]


def test_node_still_reserved_after_reservation_ended_gets_free_access(gw, publisher, clock, door):
    clock.advance(seconds=10)
    heartbeat(gw, mode="RESERVED", color="#00FF00")
    assert mode_commands(publisher)[-1]["params"] == {"mode": "FREE_ACCESS"}


def test_legacy_heartbeat_without_state_is_not_reconciled(gw, publisher, clock, reserved_green):
    before = len(mode_commands(publisher))
    clock.advance(seconds=10)
    heartbeat(gw)
    assert len(mode_commands(publisher)) == before


def test_resend_is_rate_limited_until_the_next_window(gw, publisher, clock, reserved_green):
    before = len(mode_commands(publisher))
    clock.advance(seconds=10)
    heartbeat(gw, mode="FREE_ACCESS")
    clock.advance(seconds=1)
    heartbeat(gw, mode="FREE_ACCESS")
    assert len(mode_commands(publisher)) == before + 1
    clock.advance(seconds=gw.config.mode_resync_interval_s)
    heartbeat(gw, mode="FREE_ACCESS")
    assert len(mode_commands(publisher)) == before + 2


def test_node_in_maintenance_is_not_reconciled(gw, publisher, clock, reserved_green):
    gw.devices.set_maintenance(DOOR, True, minutes=30)
    before = len(mode_commands(publisher))
    clock.advance(seconds=10)
    heartbeat(gw, mode="FREE_ACCESS")
    assert len(mode_commands(publisher)) == before


def test_invalid_reported_mode_is_rejected_by_the_contract(gw, door):
    assert heartbeat(gw, mode="OPEN") is False
