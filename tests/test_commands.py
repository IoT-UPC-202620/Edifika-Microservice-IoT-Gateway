import json
import threading

from edge_gateway.db import database
from edge_gateway.models import Outbox


def ack(gw, device_id, command_id, status="OK", detail=None):
    gw.commands.handle_ack(device_id, {"commandId": command_id, "status": status, "detail": detail})


def events():
    return [(e.kind, json.loads(e.payload)) for e in Outbox.select().order_by(Outbox.id)]


def test_send_publishes_a_versioned_command_and_tracks_it(gw, publisher):
    pending = gw.commands.send("d1", "unlock", {"durationMs": 5000})
    device_id, command = publisher.sent[0]
    assert device_id == "d1"
    assert command["v"] == 1 and command["type"] == "unlock" and command["params"] == {"durationMs": 5000}
    assert command["commandId"] == pending.command_id
    assert len(gw.commands.pending) == 1


def test_ack_clears_the_pending_command_without_noise(gw):
    pending = gw.commands.send("d1", "unlock")
    ack(gw, "d1", pending.command_id)
    assert len(gw.commands.pending) == 0
    assert Outbox.select().count() == 0


def test_failed_ack_is_reported_to_the_cloud(gw):
    pending = gw.commands.send("d1", "unlock")
    ack(gw, "d1", pending.command_id, "FAILED", "relay stuck")
    ((kind, payload),) = events()
    assert kind == "command_result" and payload["status"] == "FAILED" and payload["detail"] == "relay stuck"


def test_ack_from_another_device_is_ignored(gw):
    pending = gw.commands.send("d1", "unlock")
    ack(gw, "d2", pending.command_id)
    assert Outbox.select().count() == 0


def test_unanswered_lock_command_raises_a_lock_alert(gw, clock):
    gw.commands.send("d1", "access_result", {"result": "GRANTED"})
    clock.advance(seconds=2)
    assert gw.commands.check_timeouts() == 0
    clock.advance(seconds=2)
    assert gw.commands.check_timeouts() == 1
    assert [k for k, _ in events()] == ["command_result", "alert"]
    assert events()[1][1]["alert"] == "lock_unresponsive"
    assert gw.commands.check_timeouts() == 0  # reported only once


def test_unanswered_non_lock_command_only_reports_the_timeout(gw, clock):
    gw.commands.send("d1", "display", {"message": "hi"})
    clock.advance(seconds=10)
    gw.commands.check_timeouts()
    assert [k for k, _ in events()] == ["command_result"]


def test_publish_failure_does_not_leave_a_phantom_pending_command(gw, publisher):
    publisher.fail = True
    try:
        gw.commands.send("d1", "unlock")
    except RuntimeError:
        pass
    assert len(gw.commands.pending) == 0


def test_send_and_wait_returns_when_the_ack_arrives(gw, publisher):
    result = {}

    def run():
        with database.connection_context():
            result["pending"] = gw.commands.send_and_wait("d1", "unlock", timeout=2)

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(200):
        if publisher.sent:
            break
        threading.Event().wait(0.01)
    ack(gw, "d1", publisher.sent[0][1]["commandId"])
    thread.join(3)
    assert result["pending"].status == "OK"


def test_send_and_wait_returns_none_on_timeout_and_cleans_up(gw):
    assert gw.commands.send_and_wait("d1", "unlock", timeout=0.05) is None
    assert len(gw.commands.pending) == 0
