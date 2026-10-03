import json
from datetime import datetime, timedelta

import pytest

from edge_gateway.models import AccessAttempt, AccessPermission, AreaSchedule, Credential, Outbox
from edge_gateway.services.errors import ConflictError

from .conftest import AUTH

UID = "04A1B2C3"


def tap(gw, device_id="door-1", uid=UID, credential_type="RFID"):
    gw.access.on_access(device_id, {"credential": uid, "credentialType": credential_type, "ts": 0})


def events(kind=None):
    rows = [(e.kind, json.loads(e.payload)) for e in Outbox.select().order_by(Outbox.id)]
    return [p for k, p in rows if kind is None or k == kind]


def last_answer(publisher):
    return publisher.commands("door-1", "access_result")[-1]["params"]


@pytest.fixture
def door(gw, clock):
    """A registered, active door node in the pool area with a resident holding a reservation."""
    device = gw.devices.register("door-1", area_id="pool", capabilities=["rfid", "lock", "oled", "buzzer"])
    gw.devices.touch(device)
    now = clock.now()
    Credential.create(
        uid=UID, resident_id="r1", resident_name="Ana Perez", updated_at=now
    )
    AccessPermission.create(
        source_id="res-1", resident_id="r1", area_id="pool",
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
    )
    Outbox.delete().execute()  # start each test with an empty queue
    return device


# --- US71: read a card and resolve the access -------------------------------------------
def test_valid_card_with_reservation_is_granted_and_the_lock_is_opened(gw, door, publisher):
    tap(gw)
    answer = last_answer(publisher)
    assert answer["result"] == "GRANTED" and answer["lockMs"] == 5000
    assert answer["name"] == "Ana Perez" and answer["buzzer"] == "granted"
    assert answer["message"] == "Access granted"
    attempt = AccessAttempt.get()
    assert (attempt.result, attempt.reason, attempt.resident_id) == ("GRANTED", "ok", "r1")


def test_decision_is_taken_without_any_cloud_call(gw, door):
    # The gateway has no cloud client at all in this fixture: the cache is enough.
    tap(gw)
    assert AccessAttempt.get().result == "GRANTED"


@pytest.mark.parametrize(
    "uid, reason, message",
    [("DEADBEEF", "unknown_credential", "Card not registered")],
)
def test_unknown_card_is_denied(gw, door, publisher, uid, reason, message):
    tap(gw, uid=uid)
    answer = last_answer(publisher)
    assert answer["result"] == "DENIED" and answer["reason"] == reason
    assert answer["message"] == message and answer["buzzer"] == "denied"
    assert "lockMs" not in answer


@pytest.mark.parametrize("status", ["SUSPENDED", "REVOKED"])
def test_blocked_card_is_denied(gw, door, publisher, status):
    Credential.update(status=status).execute()
    tap(gw)
    assert last_answer(publisher)["reason"] == f"credential_{status.lower()}"


def test_expired_credential_is_denied(gw, door, publisher, clock):
    Credential.update(valid_until=clock.now() - timedelta(days=1)).execute()
    tap(gw)
    assert last_answer(publisher)["reason"] == "credential_expired"


def test_card_without_reservation_for_the_area_is_denied(gw, door, publisher):
    AccessPermission.delete().execute()
    tap(gw)
    assert last_answer(publisher)["reason"] == "no_permission"


def test_reservation_window_that_already_ended_is_denied(gw, door, publisher, clock):
    clock.advance(hours=2)
    gw.devices.touch(door)
    tap(gw)
    assert last_answer(publisher)["reason"] == "no_permission"


def test_reservation_for_another_area_does_not_open_this_door(gw, door, publisher):
    AccessPermission.update(area_id="gym").execute()
    tap(gw)
    assert last_answer(publisher)["reason"] == "no_permission"


def test_door_without_an_area_only_needs_an_active_credential(gw, publisher):
    device = gw.devices.register("gate", capabilities=["rfid"])
    gw.devices.touch(device)
    Credential.create(uid=UID, resident_id="r1", resident_name="Ana", updated_at=gw.clock.now())
    tap(gw, "gate")
    assert publisher.commands("gate", "access_result")[-1]["params"]["result"] == "GRANTED"


def test_card_is_matched_case_insensitively(gw, door, publisher):
    tap(gw, uid=" 04a1b2c3 ")
    assert last_answer(publisher)["result"] == "GRANTED"


def test_card_held_in_front_of_the_reader_counts_once(gw, door, publisher, clock):
    tap(gw)
    clock.advance(seconds=1)
    tap(gw)
    assert AccessAttempt.select().count() == 1
    assert len(publisher.commands("door-1", "access_result")) == 1
    clock.advance(seconds=3)
    tap(gw)
    assert AccessAttempt.select().count() == 2


def test_mobile_credentials_are_not_supported_yet(gw, door, publisher):
    tap(gw, credential_type="MOBILE")
    assert last_answer(publisher)["reason"] == "unsupported_credential_type"


def test_unregistered_reader_is_ignored(gw, publisher):
    tap(gw, "ghost")
    assert AccessAttempt.select().count() == 0
    assert publisher.sent == []


def test_denied_access_does_not_disturb_the_node_if_the_broker_is_down(gw, door, publisher):
    publisher.fail = True
    tap(gw)
    assert AccessAttempt.get().result == "GRANTED"  # decision is stored even if the answer failed


# --- US87: opening hours per area ------------------------------------------------------
def test_access_outside_the_area_schedule_is_denied(gw, door, publisher, clock):
    AreaSchedule.create(area_id="pool", opens_at="06:00", closes_at="22:00")
    clock.advance(hours=13, minutes=10)  # 23:10 in Lima
    gw.devices.touch(door)
    AccessPermission.update(ends_at=clock.now() + timedelta(hours=1), starts_at=clock.now() - timedelta(hours=1)).execute()
    tap(gw)
    assert last_answer(publisher)["reason"] == "outside_schedule"


def test_access_inside_the_area_schedule_is_granted(gw, door, publisher):
    AreaSchedule.create(area_id="pool", opens_at="06:00", closes_at="22:00")
    tap(gw)  # 10:00 in Lima
    assert last_answer(publisher)["result"] == "GRANTED"


def test_overnight_schedule_wraps_around_midnight(gw, door, publisher, clock):
    AreaSchedule.create(area_id="pool", opens_at="20:00", closes_at="02:00")
    clock.advance(hours=15)  # 01:00 in Lima
    gw.devices.touch(door)
    AccessPermission.update(ends_at=clock.now() + timedelta(hours=1), starts_at=clock.now() - timedelta(hours=1)).execute()
    tap(gw)
    assert last_answer(publisher)["result"] == "GRANTED"


# --- US89 interplay: node in maintenance -----------------------------------------------
def test_node_in_maintenance_denies_every_card_and_keeps_the_lock_closed(gw, door, publisher):
    door.status = "MAINTENANCE"
    door.save()
    tap(gw)
    answer = last_answer(publisher)
    assert answer["result"] == "DENIED" and answer["reason"] == "maintenance"
    assert answer["message"] == "Under maintenance" and "lockMs" not in answer
    assert AccessAttempt.get().reason == "maintenance"  # the attempt is still recorded


# --- US72: lock acknowledgement --------------------------------------------------------
def test_unconfirmed_unlock_raises_a_lock_alert_but_unconfirmed_denial_does_not(gw, door, publisher, clock):
    tap(gw)  # granted -> lock expected to move
    tap(gw, uid="DEADBEEF")  # denied -> no lock movement
    clock.advance(seconds=4)
    gw.commands.check_timeouts()
    alerts = [e["alert"] for e in events("alert")]
    assert alerts == ["lock_unresponsive"]


def test_confirmed_unlock_produces_no_alert(gw, door, publisher, clock):
    tap(gw)
    gw.commands.handle_ack("door-1", {"commandId": publisher.sent[-1][1]["commandId"], "status": "OK"})
    clock.advance(seconds=10)
    gw.commands.check_timeouts()
    assert events("alert") == []


# --- US75: every attempt is queued for the cloud, also while offline --------------------
def test_each_attempt_is_queued_with_its_context(gw, door):
    tap(gw)
    (event,) = events("access_attempt")
    assert event["result"] == "GRANTED" and event["areaId"] == "pool" and event["residentId"] == "r1"
    assert event["credential"] == UID and event["deviceId"] == "door-1"


# --- possible unauthorized access -------------------------------------------------------
def test_three_denied_attempts_in_a_row_raise_one_alert(gw, door, publisher, clock):
    for _ in range(5):
        tap(gw, uid="DEADBEEF")
        clock.advance(seconds=5)
    alerts = events("alert")
    assert [a["alert"] for a in alerts] == ["possible_unauthorized_access"]
    assert alerts[0]["attempts"] == 3 and alerts[0]["credential"] == "DEADBEEF"


def test_a_granted_attempt_resets_the_denial_streak(gw, door, clock):
    Credential.update(status="SUSPENDED").execute()
    for _ in range(2):
        tap(gw)
        clock.advance(seconds=5)
    Credential.update(status="ACTIVE").execute()
    tap(gw)
    clock.advance(seconds=5)
    Credential.update(status="SUSPENDED").execute()
    for _ in range(2):
        tap(gw)
        clock.advance(seconds=5)
    assert events("alert") == []


# --- manual credential administration (US86 offline path) and access log (US55/US93) --
def test_manual_registration_is_active_immediately_and_queued_for_the_cloud(gw, door, publisher):
    gw.credentials.register("aa11bb22", "r2", "Luis Ramos")
    assert Credential.get_by_id("AA11BB22").status == "ACTIVE"
    (event,) = events("credential_changed")
    assert event["source"] == "edge_manual" and event["residentId"] == "r2"


def test_manual_registration_of_a_card_owned_by_another_resident_is_rejected(gw, door):
    with pytest.raises(ConflictError, match="already in use"):
        gw.credentials.register(UID, "someone-else")
    assert Credential.get_by_id(UID).resident_id == "r1"


def test_api_registers_blocks_and_lists_credentials(client):
    created = client.post(
        "/api/v1/credentials", headers=AUTH,
        json={"uid": "aa11", "residentId": "r9", "residentName": "Eva", "validUntil": "2030-01-01T00:00:00Z"},
    )
    assert created.status_code == 201 and created.get_json()["uid"] == "AA11"
    blocked = client.patch("/api/v1/credentials/aa11/status", headers=AUTH, json={"status": "REVOKED"})
    assert blocked.get_json()["status"] == "REVOKED"
    assert client.patch("/api/v1/credentials/zzzz/status", headers=AUTH, json={"status": "ACTIVE"}).status_code == 404
    assert client.patch("/api/v1/credentials/aa11/status", headers=AUTH, json={"status": "X"}).status_code == 422
    assert [c["uid"] for c in client.get("/api/v1/credentials", headers=AUTH).get_json()] == ["AA11"]


def test_api_rejects_a_conflicting_card_and_bad_dates(client):
    client.post("/api/v1/credentials", headers=AUTH, json={"uid": "aa11", "residentId": "r1"})
    assert client.post("/api/v1/credentials", headers=AUTH, json={"uid": "aa11", "residentId": "r2"}).status_code == 409
    bad = client.post("/api/v1/credentials", headers=AUTH, json={"uid": "bb", "residentId": "r1", "validFrom": "tomorrow"})
    assert bad.status_code == 422


def test_api_access_log_is_filterable_and_masks_credentials(gw, door, client, clock):
    tap(gw)
    clock.advance(seconds=5)
    tap(gw, uid="DEADBEEF")
    rows = client.get("/api/v1/access-attempts", headers=AUTH).get_json()
    assert [r["result"] for r in rows] == ["DENIED", "GRANTED"]  # newest first
    assert rows[1]["credential"] == "****B2C3" and UID not in json.dumps(rows)
    denied = client.get("/api/v1/access-attempts?result=DENIED&deviceId=door-1", headers=AUTH).get_json()
    assert len(denied) == 1
    assert client.get("/api/v1/access-attempts?limit=0", headers=AUTH).status_code == 422
    assert client.get("/api/v1/access-attempts").status_code == 401
