from datetime import timedelta
import json
import pytest

from edge_gateway.models import AccessPermission, Credential, Device, DoorReservation, Outbox
from edge_gateway.services.access import DENIED, GRANTED
from tests.helpers import msg

PREFIX = "edifika/v1"


def outbox_payloads(kind):
    return [json.loads(row.payload) for row in Outbox.select().where(Outbox.kind == kind)]


@pytest.fixture
def door_node(gw):
    device = Device.create(
        device_id="esp32-acc-01",
        name="Puerta piscina",
        location="Piso 1",
        capabilities='["lock", "rfid", "oled", "buzzer", "rgb_led"]',
        status="ACTIVE",
        managed="cloud",
        current_mode="FREE_ACCESS",
    )
    return device


def test_free_access_grants_active_card(gw, publisher, door_node, clock):
    Credential.create(uid="04A1B2C3", resident_id="15", resident_name="Ana", status="ACTIVE", updated_at=clock.now())
    gw.router.handle(*msg(PREFIX, "esp32-acc-01", "access", credential="04A1B2C3"))

    assert publisher.commands("esp32-acc-01", "access_result")[-1]["params"]["result"] == GRANTED
    payload = outbox_payloads("access_attempt")[-1]
    assert payload["result"] == GRANTED
    assert payload["accessMode"] == "FREE_ACCESS"


def test_free_access_denies_suspended_card(gw, publisher, door_node, clock):
    Credential.create(uid="04A1B2C3", resident_id="15", resident_name="Ana", status="SUSPENDED", updated_at=clock.now())
    gw.router.handle(*msg(PREFIX, "esp32-acc-01", "access", credential="04A1B2C3"))

    params = publisher.commands("esp32-acc-01", "access_result")[-1]["params"]
    assert params["result"] == DENIED
    assert params["reason"] == "credential_suspended"


def test_reserved_denies_resident_outside_reservation(gw, publisher, door_node, clock):
    now = clock.now()
    DoorReservation.create(
        source_id="res-1",
        device_id="esp32-acc-01",
        holder_resident_id="15",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=55),
    )
    door_node.current_mode = "RESERVED"
    door_node.save()

    Credential.create(uid="99999999", resident_id="99", resident_name="Stranger", status="ACTIVE", updated_at=now)
    gw.router.handle(*msg(PREFIX, "esp32-acc-01", "access", credential="99999999"))

    params = publisher.commands("esp32-acc-01", "access_result")[-1]["params"]
    assert params["result"] == DENIED
    assert params["reason"] == "not_authorized"
    assert params["message"] == "Not authorized"


def test_reserved_grants_holder_and_guest(gw, publisher, door_node, clock):
    now = clock.now()
    DoorReservation.create(
        source_id="res-1",
        device_id="esp32-acc-01",
        holder_resident_id="15",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=55),
    )
    AccessPermission.create(
        source_id="res:res-1:15",
        device_id="esp32-acc-01",
        resident_id="15",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=55),
    )
    AccessPermission.create(
        source_id="res:res-1:16",
        device_id="esp32-acc-01",
        resident_id="16",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=55),
    )
    door_node.current_mode = "RESERVED"
    door_node.save()

    Credential.create(uid="04A1B2C3", resident_id="15", resident_name="Holder", status="ACTIVE", updated_at=now)
    Credential.create(uid="04A1B2C4", resident_id="16", resident_name="Guest", status="ACTIVE", updated_at=now)

    gw.router.handle(*msg(PREFIX, "esp32-acc-01", "access", credential="04A1B2C3"))
    assert publisher.commands("esp32-acc-01", "access_result")[-1]["params"]["result"] == GRANTED

    gw.router.handle(*msg(PREFIX, "esp32-acc-01", "access", credential="04A1B2C4"))
    assert publisher.commands("esp32-acc-01", "access_result")[-1]["params"]["result"] == GRANTED


def test_mode_switches_at_start_and_end_without_sync(gw, publisher, clock, door_node):
    now = clock.now()
    DoorReservation.create(
        source_id="res-1",
        device_id="esp32-acc-01",
        holder_resident_id="15",
        starts_at=now + timedelta(minutes=1),
        ends_at=now + timedelta(minutes=2),
    )
    assert gw.access.check_mode_changes() == []

    clock.advance(minutes=1)
    assert gw.access.check_mode_changes() == ["esp32-acc-01"]
    assert publisher.commands("esp32-acc-01", "mode")[-1]["params"] == {"mode": "RESERVED"}

    Device.update(led_color="#FF00AA").where(Device.device_id == "esp32-acc-01").execute()
    clock.advance(minutes=1)
    gw.access.check_mode_changes()
    assert publisher.commands("esp32-acc-01", "mode")[-1]["params"] == {"mode": "FREE_ACCESS"}
    assert Device.get_by_id("esp32-acc-01").led_color is None
    assert [e["event"] for e in outbox_payloads("device_status")][-2:] == ["mode_changed", "mode_changed"]
