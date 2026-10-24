import json

import pytest

from edge_gateway.clock import to_epoch
from edge_gateway.models import ActiveAlert, Device, Outbox
from edge_gateway.scheduler import Scheduler
from edge_gateway.services.errors import ConflictError, ValidationFailed

from .conftest import AUTH


def hb(gw, device_id="d1", ts=None, fw=None):
    ts = to_epoch(gw.clock.now()) if ts is None else ts
    gw.devices.on_heartbeat(device_id, {"ts": ts, "fw": fw})


def alerts():
    return [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "alert"]


@pytest.fixture
def d1(gw):
    return gw.devices.register("d1", area_id="pool", capabilities=["rfid", "lock"])


# --- registry (US79) ------------------------------------------------------------------
def test_new_device_starts_inactive_and_is_reported(gw, d1):
    assert d1.status == "INACTIVE" and d1.capability_list == ["lock", "rfid"]
    event = json.loads(Outbox.get().payload)
    assert event["event"] == "registered" and event["deviceId"] == "d1"


def test_duplicate_device_is_rejected(gw, d1):
    with pytest.raises(ConflictError, match="already registered"):
        gw.devices.register("d1")
    assert Device.select().count() == 1


def test_unknown_capability_is_rejected(gw):
    with pytest.raises(ValidationFailed):
        gw.devices.register("d9", capabilities=["laser"])


def test_messages_from_unregistered_devices_are_ignored_and_reported_once(gw):
    hb(gw, "ghost")
    hb(gw, "ghost")
    assert Device.select().count() == 0
    assert [a["alert"] for a in alerts()] == ["unregistered_device"]


# --- liveness (US80) ------------------------------------------------------------------
def test_first_heartbeat_activates_the_device(gw, d1):
    hb(gw, fw="1.0.2")
    device = Device.get_by_id("d1")
    assert device.status == "ACTIVE" and device.firmware == "1.0.2"
    assert device.last_seen == gw.clock.now()


def test_silent_device_goes_offline_and_pending_commands_are_dropped(gw, d1, clock):
    hb(gw)
    gw.commands.send("d1", "unlock")
    clock.advance(seconds=29)
    assert gw.devices.check_offline() == []
    clock.advance(seconds=2)
    assert gw.devices.check_offline() == ["d1"]
    assert Device.get_by_id("d1").status == "OFFLINE"
    assert len(gw.commands.pending) == 0
    (alert,) = alerts()
    assert alert["alert"] == "device_offline" and alert["state"] == "raised"
    assert gw.devices.check_offline() == []  # reported once


def test_other_devices_are_not_affected_by_one_going_offline(gw, d1, clock):
    gw.devices.register("d2")
    hb(gw, "d1")
    clock.advance(seconds=20)
    hb(gw, "d2")
    clock.advance(seconds=15)
    assert gw.devices.check_offline() == ["d1"]
    assert Device.get_by_id("d2").status == "ACTIVE"


def test_device_that_never_sent_a_heartbeat_is_not_flagged_offline(gw, d1, clock):
    clock.advance(hours=1)
    assert gw.devices.check_offline() == []


def test_reconnection_marks_active_and_clears_the_alert(gw, d1, clock):
    hb(gw)
    clock.advance(seconds=60)
    gw.devices.check_offline()
    hb(gw)
    assert Device.get_by_id("d1").status == "ACTIVE"
    assert [a["state"] for a in alerts()] == ["raised", "cleared"]
    assert ActiveAlert.select().count() == 0


# --- clock sync (US85) ----------------------------------------------------------------
def test_drifted_node_clock_triggers_a_time_sync_command(gw, d1, publisher):
    hb(gw, ts=to_epoch(gw.clock.now()) - 3600)
    (command,) = publisher.commands("d1", "time_sync")
    assert command["params"]["epoch"] == to_epoch(gw.clock.now())
    assert command["params"]["timezone"] == "America/Lima"


def test_small_drift_is_tolerated(gw, d1, publisher):
    hb(gw, ts=to_epoch(gw.clock.now()) + 1.5)
    assert publisher.commands(type_="time_sync") == []


def test_time_sync_is_not_repeated_within_the_cooldown(gw, d1, publisher, clock):
    bad = to_epoch(gw.clock.now()) - 100
    hb(gw, ts=bad)
    hb(gw, ts=bad)
    assert len(publisher.commands(type_="time_sync")) == 1
    clock.advance(seconds=31)
    hb(gw, ts=bad)
    assert len(publisher.commands(type_="time_sync")) == 2


def test_node_booting_with_an_invalid_clock_is_synchronised(gw, d1, publisher):
    hb(gw, ts=0)
    assert len(publisher.commands("d1", "time_sync")) == 1


# --- scheduler ------------------------------------------------------------------------
def test_scheduler_runs_each_task_on_its_interval_and_survives_failures():
    calls = {"ok": 0, "bad": 0}
    t = [0.0]

    def bad():
        calls["bad"] += 1
        raise RuntimeError("boom")

    def ok():
        calls["ok"] += 1

    scheduler = Scheduler([("bad", 1, bad), ("ok", 2, ok)], monotonic=lambda: t[0])
    scheduler.run_due()
    t[0] = 1.0
    scheduler.run_due()
    t[0] = 2.0
    scheduler.run_due()
    assert calls == {"ok": 2, "bad": 3}


# --- REST API -------------------------------------------------------------------------
def test_api_requires_the_bearer_token(client):
    assert client.get("/api/v1/devices").status_code == 401
    assert client.get("/api/v1/devices", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_api_registers_lists_and_gets_devices(client):
    body = {"deviceId": "esp32-door-01", "areaId": "pool", "capabilities": ["rfid", "lock", "oled"]}
    created = client.post("/api/v1/devices", json=body, headers=AUTH)
    assert created.status_code == 201
    assert created.get_json()["status"] == "INACTIVE"
    assert client.get("/api/v1/devices", headers=AUTH).get_json()[0]["deviceId"] == "esp32-door-01"
    one = client.get("/api/v1/devices/esp32-door-01", headers=AUTH)
    assert one.status_code == 200 and one.get_json()["areaId"] == "pool"


def test_api_duplicate_registration_returns_409(client):
    body = {"deviceId": "d1"}
    client.post("/api/v1/devices", json=body, headers=AUTH)
    response = client.post("/api/v1/devices", json=body, headers=AUTH)
    assert response.status_code == 409
    assert "already registered" in response.get_json()["message"]


def test_api_unknown_device_returns_404(client):
    assert client.get("/api/v1/devices/nope", headers=AUTH).status_code == 404


def test_api_rejects_invalid_body(client):
    assert client.post("/api/v1/devices", json={}, headers=AUTH).status_code == 422


# --- Task 7: device status events -----------------------------------------------------
def test_device_back_online_emits_online(gw, clock):
    gw.devices.register("d-test")
    # Inactive -> Active
    gw.devices.on_heartbeat("d-test", {"ts": to_epoch(clock.now())})
    status_events = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "device_status"]
    assert any(e.get("deviceId") == "d-test" and e.get("event") == "online" for e in status_events)

    # Active -> Offline
    clock.advance(seconds=60)
    gw.devices.check_offline()

    # Offline -> Active
    clock.advance(seconds=1)
    gw.devices.on_heartbeat("d-test", {"ts": to_epoch(clock.now())})
    status_events = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "device_status"]
    assert status_events[-1]["deviceId"] == "d-test" and status_events[-1]["event"] == "online"


def test_silent_device_emits_offline(gw, clock):
    gw.devices.register("d-silent")
    gw.devices.on_heartbeat("d-silent", {"ts": to_epoch(clock.now())})
    clock.advance(seconds=60)
    gw.devices.check_offline()
    status_events = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "device_status"]
    offline_ev = [e for e in status_events if e.get("event") == "offline"]
    assert len(offline_ev) == 1
    assert offline_ev[0]["deviceId"] == "d-silent"
    assert "lastSeen" in offline_ev[0]


def test_firmware_change_emits_once(gw):
    gw.devices.register("d-fw")
    # First heartbeat with firmware v1.0.0
    gw.devices.on_heartbeat("d-fw", {"ts": to_epoch(gw.clock.now()), "fw": "v1.0.0"})
    status_events = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "device_status"]
    fw_events = [e for e in status_events if e.get("event") == "firmware"]
    assert len(fw_events) == 1
    assert fw_events[0] == {"deviceId": "d-fw", "event": "firmware", "firmware": "v1.0.0"}

    # Second heartbeat with same firmware v1.0.0 -> no new event
    gw.devices.on_heartbeat("d-fw", {"ts": to_epoch(gw.clock.now()), "fw": "v1.0.0"})
    status_events2 = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "device_status"]
    fw_events2 = [e for e in status_events2 if e.get("event") == "firmware"]
    assert len(fw_events2) == 1

    # Third heartbeat with upgraded firmware v1.1.0 -> emits new event
    gw.devices.on_heartbeat("d-fw", {"ts": to_epoch(gw.clock.now()), "fw": "v1.1.0"})
    status_events3 = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "device_status"]
    fw_events3 = [e for e in status_events3 if e.get("event") == "firmware"]
    assert len(fw_events3) == 2
    assert fw_events3[1] == {"deviceId": "d-fw", "event": "firmware", "firmware": "v1.1.0"}
