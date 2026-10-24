import logging

import pytest

from edge_gateway.models import AccessPermission, Device, DoorReservation
from edge_gateway.services.sync import SyncSchema


def door(
    device_code="esp32-acc-01",
    name="Puerta piscina",
    capabilities=None,
    led_color=None,
):
    return {
        "deviceCode": device_code,
        "name": name,
        "location": "Piso 1",
        "capabilities": capabilities
        or [
            "RFID",
            "LOCK",
            "BUZZER",
            "OLED",
            "RGB_LED",
            "TEMPERATURE",
            "HUMIDITY",
        ],
        "ledColor": led_color,
        "status": "ACTIVE",
        "settings": {},
    }


def resv(
    res_id="7",
    device_code="esp32-acc-01",
    holder="15",
    guests=None,
    starts_at=None,
    ends_at=None,
):
    return {
        "id": res_id,
        "deviceCode": device_code,
        "holderResidentId": holder,
        "startsAt": starts_at or "2026-10-23T20:00:00Z",
        "endsAt": ends_at or "2026-10-23T22:00:00Z",
        "authorizedResidentIds": guests or [],
    }


def snapshot(
    version=1,
    full=True,
    devices=None,
    reservations=None,
    credentials=None,
    removed_devices=None,
    removed_reservations=None,
):
    return {
        "version": version,
        "full": full,
        "devices": devices or [],
        "reservations": reservations or [],
        "credentials": credentials or [],
        "removedDeviceCodes": removed_devices or [],
        "removedReservationIds": removed_reservations or [],
    }


def load(data):
    return SyncSchema().load(data)


def test_sync_registers_cloud_devices(gw):
    gw.sync.apply(load(snapshot(devices=[door()])))
    d = Device.get_by_id("esp32-acc-01")
    assert (d.managed, d.name, d.capability_list[:3]) == (
        "cloud",
        "Puerta piscina",
        ["buzzer", "humidity", "lock"],
    ) or (d.managed, d.name, d.capability_list[:3]) == (
        "cloud",
        "Puerta piscina",
        ["rfid", "lock", "buzzer"],
    )


def test_full_snapshot_keeps_local_devices(gw):
    gw.devices.register("esp32-local")
    gw.sync.apply(load(snapshot(version=2, devices=[door()])))
    assert Device.get_or_none(Device.device_id == "esp32-local")


def test_reservation_permissions_for_holder_and_guests(gw):
    gw.sync.apply(
        load(
            snapshot(
                devices=[door()],
                reservations=[resv("7", holder="15", guests=["16", "21"])],
            )
        )
    )
    assert {
        p.resident_id
        for p in AccessPermission.select().where(
            AccessPermission.device_id == "esp32-acc-01"
        )
    } == {"15", "16", "21"}


def test_removed_reservation_drops_its_permissions(gw):
    gw.sync.apply(
        load(
            snapshot(
                devices=[door()],
                reservations=[resv("7", holder="15", guests=["16", "21"])],
            )
        )
    )
    gw.sync.apply(load(snapshot(version=2, full=False, removed_reservations=["7"])))
    assert DoorReservation.select().where(DoorReservation.source_id == "7").count() == 0
    assert (
        AccessPermission.select()
        .where(AccessPermission.device_id == "esp32-acc-01")
        .count()
        == 0
    )


def test_reservation_for_unknown_device_is_skipped(gw, caplog):
    with caplog.at_level(logging.WARNING):
        gw.sync.apply(
            load(
                snapshot(
                    reservations=[resv("8", device_code="unknown-dev", holder="15")]
                )
            )
        )
    assert DoorReservation.select().where(DoorReservation.source_id == "8").count() == 0
    assert "unknown-dev" in caplog.text


def test_removed_device_drops_only_cloud_devices(gw):
    gw.devices.register("local-dev")
    gw.sync.apply(load(snapshot(devices=[door("cloud-dev")])))
    gw.sync.apply(
        load(snapshot(version=2, full=False, removed_devices=["cloud-dev", "local-dev"]))
    )
    assert Device.get_or_none(Device.device_id == "cloud-dev") is None
    assert Device.get_or_none(Device.device_id == "local-dev") is not None
