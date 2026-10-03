import json
from dataclasses import replace
from datetime import timedelta

import pytest

from edge_gateway import create_app
from edge_gateway.clock import to_epoch
from edge_gateway.db import database
from edge_gateway.models import Device, Outbox, SensorReading

from .conftest import AUTH
from .helpers import FakePublisher


class AckingPublisher(FakePublisher):
    """A node that confirms every command as soon as it receives it."""

    def __init__(self, gateway_ref, status="OK"):
        super().__init__()
        self.gateway_ref = gateway_ref
        self.status = status

    def publish_command(self, device_id, command):
        super().publish_command(device_id, command)
        if self.status:
            self.gateway_ref().commands.handle_ack(
                device_id, {"commandId": command["commandId"], "status": self.status, "detail": None}
            )


def events(kind=None):
    rows = [(e.kind, json.loads(e.payload)) for e in Outbox.select().order_by(Outbox.id)]
    return [p for k, p in rows if kind is None or k == kind]


def alive(gw, device_id="door-1", capabilities=("rfid", "lock", "oled", "buzzer")):
    device = gw.devices.register(device_id, area_id="pool", capabilities=list(capabilities))
    gw.devices.touch(device)
    Outbox.delete().execute()
    return device


@pytest.fixture
def acking(gw):
    gw.use_publisher(AckingPublisher(lambda: gw))
    return gw.publisher.target


# --- US83: remote commands --------------------------------------------------------------
def test_remote_unlock_is_executed_confirmed_and_reported(gw, acking):
    alive(gw)
    result = gw.remote.execute("door-1", "unlock", {"durationMs": 3000}, requested_by="admin-7")
    assert result["status"] == "ACKED"
    (command,) = acking.commands("door-1", "unlock")
    assert command["params"] == {"durationMs": 3000}
    (event,) = events("command_result")
    assert event["status"] == "ACKED" and event["requestedBy"] == "admin-7" and event["source"] == "remote"


def test_remote_unlock_uses_the_default_duration(gw, acking):
    alive(gw)
    gw.remote.execute("door-1", "unlock")
    assert acking.commands("door-1", "unlock")[0]["params"] == {"durationMs": 5000}


def test_unconfirmed_command_times_out_and_is_reported(config, clock, publisher):
    app = create_app(replace(config, remote_command_timeout_s=0.05), clock=clock)
    gw = app.extensions["gateway"]
    gw.use_publisher(publisher)
    with database.connection_context():
        alive(gw)
        from edge_gateway.services.errors import CommandNotConfirmed

        with pytest.raises(CommandNotConfirmed, match="did not confirm"):
            gw.remote.execute("door-1", "unlock")
        assert events("command_result")[0]["status"] == "TIMEOUT"
        assert len(gw.commands.pending) == 0


def test_a_failure_reported_by_the_node_is_not_logged_twice(gw):
    from edge_gateway.services.errors import CommandNotConfirmed

    gw.use_publisher(AckingPublisher(lambda: gw, status="FAILED"))
    alive(gw)
    with pytest.raises(CommandNotConfirmed, match="reported a failure"):
        gw.remote.execute("door-1", "unlock")
    assert [e["status"] for e in events("command_result")] == ["FAILED"]


def test_offline_node_rejects_the_command_without_queueing_it(gw, acking, clock):
    from edge_gateway.services.errors import DeviceUnavailable

    alive(gw)
    clock.advance(minutes=5)
    gw.devices.check_offline()
    with pytest.raises(DeviceUnavailable, match="not available"):
        gw.remote.execute("door-1", "unlock")
    assert acking.commands("door-1", "unlock") == [] and len(gw.commands.pending) == 0


def test_node_that_never_connected_is_not_available(gw, acking):
    from edge_gateway.services.errors import DeviceUnavailable

    gw.devices.register("door-1", capabilities=["lock"])
    with pytest.raises(DeviceUnavailable):
        gw.remote.execute("door-1", "unlock")


def test_node_in_maintenance_rejects_every_remote_command_even_from_an_admin(gw, acking):
    from edge_gateway.services.errors import DeviceUnavailable

    alive(gw)
    gw.devices.set_maintenance("door-1", True)
    acking.sent.clear()
    with pytest.raises(DeviceUnavailable, match="under maintenance"):
        gw.remote.execute("door-1", "unlock", requested_by="admin-1")
    assert acking.sent == []


def test_command_needs_the_matching_capability(gw, acking):
    from edge_gateway.services.errors import ValidationFailed

    alive(gw, capabilities=("rfid",))
    with pytest.raises(ValidationFailed, match="no lock capability"):
        gw.remote.execute("door-1", "unlock")


@pytest.mark.parametrize(
    "type_, params",
    [("unlock", {"durationMs": 10}), ("unlock", {"durationMs": "5"}), ("display", {}),
     ("display", {"message": "x" * 65}), ("buzzer", {"pattern": "siren"})],
)
def test_invalid_command_parameters_are_rejected(gw, acking, type_, params):
    from edge_gateway.services.errors import ValidationFailed

    alive(gw)
    with pytest.raises(ValidationFailed):
        gw.remote.execute("door-1", type_, params)
    assert acking.sent == []


def test_broker_outage_is_reported_as_unavailable(gw):
    from edge_gateway.mqtt.bridge import PublishError
    from edge_gateway.services.errors import BrokerUnavailable

    class Down(FakePublisher):
        def publish_command(self, device_id, command):
            raise PublishError("not connected")

    gw.use_publisher(Down())
    alive(gw)
    with pytest.raises(BrokerUnavailable):
        gw.remote.execute("door-1", "unlock")
    assert len(gw.commands.pending) == 0


def test_api_runs_a_command_and_maps_errors_to_http_codes(gw, client, acking):
    alive(gw)
    ok = client.post("/api/v1/devices/door-1/commands", headers=AUTH, json={"type": "display", "params": {"message": "Hello"}})
    assert ok.status_code == 200 and ok.get_json()["status"] == "ACKED"
    assert client.post("/api/v1/devices/nope/commands", headers=AUTH, json={"type": "unlock"}).status_code == 404
    assert client.post("/api/v1/devices/door-1/commands", headers=AUTH, json={"type": "reboot"}).status_code == 422
    assert client.post("/api/v1/devices/door-1/commands", headers=AUTH, json={"type": "buzzer", "params": {}}).status_code == 422
    assert client.post("/api/v1/devices/door-1/commands", json={"type": "unlock"}).status_code == 401
    gw.devices.set_maintenance("door-1", True)
    blocked = client.post("/api/v1/devices/door-1/commands", headers=AUTH, json={"type": "unlock"})
    assert blocked.status_code == 409 and "maintenance" in blocked.get_json()["message"]


# --- US89: maintenance mode ---------------------------------------------------------------
def test_maintenance_deactivates_the_node_and_tells_it_to_show_the_status(gw, publisher):
    alive(gw)
    gw.commands.send("door-1", "display", {"message": "x"})
    device = gw.devices.set_maintenance("door-1", True, minutes=30, actor="admin-1")
    assert device.status == "MAINTENANCE" and device.maintenance_since == gw.clock.now()
    assert len(gw.commands.pending) == 1  # only the maintenance notice itself is tracked
    command = publisher.commands("door-1", "maintenance")[0]
    assert command["params"] == {"active": True, "message": "Under maintenance"}
    (event,) = events("device_status")
    assert event["event"] == "maintenance_started" and event["by"] == "admin-1"


def test_default_and_maximum_maintenance_duration(gw):
    alive(gw)
    device = gw.devices.set_maintenance("door-1", True)
    assert device.maintenance_until - device.maintenance_since == timedelta(minutes=120)
    from edge_gateway.services.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        gw.devices.set_maintenance("door-1", True, minutes=241)


def test_a_node_in_maintenance_raises_no_alerts_and_produces_no_readings(gw, clock):
    alive(gw)
    gw.devices.set_maintenance("door-1", True)
    Outbox.delete().execute()
    clock.advance(minutes=10)
    assert gw.devices.check_offline() == []  # silence is expected while it is being serviced
    gw.telemetry.on_reading("door-1", {"sensor": "humidity", "value": 5.0, "unit": "%", "ts": to_epoch(clock.now())})
    assert SensorReading.select().count() == 0 and events("alert") == []


def test_heartbeats_during_maintenance_do_not_reactivate_the_node(gw, publisher):
    alive(gw)
    gw.devices.set_maintenance("door-1", True)
    publisher.sent.clear()
    gw.devices.on_heartbeat("door-1", {"ts": 0, "fw": "1.1"})  # even with a wrong clock
    assert Device.get_by_id("door-1").status == "MAINTENANCE"
    assert publisher.commands(type_="time_sync") == []


def test_ending_maintenance_reactivates_a_node_that_is_still_alive(gw, publisher, clock):
    alive(gw)
    gw.devices.set_maintenance("door-1", True, actor="admin-1")
    clock.advance(minutes=10)
    gw.devices.on_heartbeat("door-1", {"ts": to_epoch(clock.now()), "fw": None})
    Outbox.delete().execute()
    device = gw.devices.set_maintenance("door-1", False, actor="admin-1")
    assert device.status == "ACTIVE" and device.maintenance_until is None
    assert publisher.commands("door-1", "maintenance")[-1]["params"] == {"active": False}
    (event,) = events("device_status")
    assert event["event"] == "maintenance_ended" and event["durationSeconds"] == 600


def test_ending_maintenance_of_a_silent_node_marks_it_offline_and_alerts(gw, clock):
    alive(gw)
    gw.devices.set_maintenance("door-1", True)
    clock.advance(minutes=10)
    Outbox.delete().execute()
    assert gw.devices.set_maintenance("door-1", False).status == "OFFLINE"
    assert [a["alert"] for a in events("alert")] == ["device_offline"]


def test_ending_maintenance_of_a_node_that_never_connected_leaves_it_inactive(gw):
    gw.devices.register("new-1")
    gw.devices.set_maintenance("new-1", True)
    assert gw.devices.set_maintenance("new-1", False).status == "INACTIVE"


def test_ending_maintenance_that_was_not_started_is_a_conflict(gw):
    from edge_gateway.services.errors import ConflictError

    alive(gw)
    with pytest.raises(ConflictError, match="not in maintenance"):
        gw.devices.set_maintenance("door-1", False)


def test_maintenance_expires_by_itself(gw, clock):
    alive(gw)
    gw.devices.set_maintenance("door-1", True, minutes=30)
    clock.advance(minutes=29)
    assert gw.devices.expire_maintenance() == []
    clock.advance(minutes=2)
    gw.devices.touch(Device.get_by_id("door-1"))  # the node is back and talking
    assert gw.devices.expire_maintenance() == ["door-1"]
    assert Device.get_by_id("door-1").status == "ACTIVE"
    assert events("device_status")[-1]["by"] == "system:expired"


def test_housekeeping_ends_expired_maintenance(gw, clock):
    alive(gw)
    gw.devices.set_maintenance("door-1", True, minutes=5)
    clock.advance(minutes=6)
    gw.housekeeping()
    assert Device.get_by_id("door-1").status != "MAINTENANCE"


def test_api_toggles_maintenance(gw, client):
    alive(gw)
    on = client.put("/api/v1/devices/door-1/maintenance", headers=AUTH, json={"enabled": True, "durationMinutes": 45})
    assert on.status_code == 200 and on.get_json()["status"] == "MAINTENANCE" and on.get_json()["maintenanceUntil"]
    off = client.put("/api/v1/devices/door-1/maintenance", headers=AUTH, json={"enabled": False})
    assert off.status_code == 200 and off.get_json()["status"] != "MAINTENANCE"
    assert client.put("/api/v1/devices/door-1/maintenance", headers=AUTH, json={"enabled": False}).status_code == 409
    assert client.put("/api/v1/devices/door-1/maintenance", headers=AUTH, json={"enabled": True, "durationMinutes": 9999}).status_code == 422
    assert client.put("/api/v1/devices/nope/maintenance", headers=AUTH, json={"enabled": True}).status_code == 404
    assert client.put("/api/v1/devices/door-1/maintenance", json={"enabled": True}).status_code == 401
