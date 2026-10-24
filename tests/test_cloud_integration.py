import json
from datetime import timedelta

import pytest
import requests

from edge_gateway import create_app
from edge_gateway.clock import to_iso
from edge_gateway.config import Config
from edge_gateway.db import database
from edge_gateway.models import (
    AccessAttempt,
    AccessPermission,
    AreaSchedule,
    Credential,
    Outbox,
    SyncState,
)
from edge_gateway.services.cloud import CloudClient, CloudRejected, CloudUnavailable
from edge_gateway.services.outbox import MAX_REJECTIONS
from edge_gateway.services.sync import SyncSchema

from .conftest import AUTH, TOKEN
from .helpers import FakeCloud, FakeClock, FakePublisher


@pytest.fixture
def cloud():
    return FakeCloud()


@pytest.fixture
def cgw(config, clock, publisher, cloud):
    """A gateway wired to a fake cloud."""
    app = create_app(config, clock=clock, cloud=cloud)
    app.extensions["gateway"].use_publisher(publisher)
    with database.connection_context():
        yield app.extensions["gateway"]


def queue(gw, n, kind="access_attempt"):
    for i in range(n):
        gw.outbox.enqueue(kind, {"n": i})


def snapshot(version=1, **overrides):
    now = FakeClock().now()
    body = {
        "version": version,
        "full": True,
        "credentials": [
            {"uid": "04a1b2c3", "type": "RFID", "residentId": "r1", "residentName": "Ana Perez", "status": "ACTIVE"}
        ],
        "permissions": [
            {"id": "res-1", "residentId": "r1", "areaId": "pool",
             "startsAt": to_iso(now - timedelta(hours=1)), "endsAt": to_iso(now + timedelta(hours=1))}
        ],
        "areaSchedules": [{"areaId": "pool", "opensAt": "06:00", "closesAt": "22:00"}],
    }
    body.update(overrides)
    return body


# --- US82 / TS33: forwarding ------------------------------------------------------------
def test_events_are_delivered_in_chronological_order_and_marked_sent(cgw, cloud):
    queue(cgw, 5)
    assert cgw.forwarder.flush() == 5
    assert [e["payload"]["n"] for e in cloud.delivered] == [0, 1, 2, 3, 4]
    assert cgw.outbox.pending_count() == 0
    assert {"eventId", "kind", "occurredAt", "payload"} <= set(cloud.delivered[0])


def test_large_backlogs_are_sent_in_batches(cgw, cloud):
    queue(cgw, 120)  # batch size is 50
    assert cgw.forwarder.flush() == 120
    assert cloud.calls == 3


def test_nothing_is_lost_during_an_outage_and_everything_arrives_afterwards(cgw, cloud, clock):
    cloud.down = True
    queue(cgw, 3)
    assert cgw.forwarder.flush() == 0
    assert cgw.outbox.pending_count() == 3
    assert cgw.forwarder.stats()["consecutiveFailures"] == 1
    assert "connection refused" in cgw.forwarder.stats()["lastError"]
    cloud.down = False
    queue(cgw, 2)
    clock.advance(seconds=5)  # past the backoff
    assert cgw.forwarder.flush() == 5
    assert [e["payload"]["n"] for e in cloud.delivered] == [0, 1, 2, 0, 1]  # order of creation, no gaps
    assert cgw.forwarder.stats()["consecutiveFailures"] == 0


def test_retries_back_off_instead_of_hammering_the_backend(cgw, cloud, clock):
    cloud.down = True
    queue(cgw, 1)
    cgw.forwarder.flush()
    cgw.forwarder.flush()  # still inside the backoff window
    assert cloud.calls == 1
    clock.advance(seconds=3)
    cgw.forwarder.flush()
    assert cloud.calls == 2


def test_backoff_is_capped(cgw, cloud, clock):
    cloud.down = True
    queue(cgw, 1)
    for _ in range(12):
        cgw.forwarder.flush()
        clock.advance(minutes=2)
    assert cgw.forwarder.next_attempt_at - clock.now() <= timedelta(seconds=60)


def test_partially_accepted_batch_keeps_the_rest_pending(cgw, cloud):
    queue(cgw, 4)
    cloud.accept_only = 2
    assert cgw.forwarder.flush() == 2
    assert cgw.outbox.pending_count() == 2


def test_resending_an_already_delivered_event_does_not_duplicate_it(cgw, cloud):
    queue(cgw, 2)
    cgw.forwarder.flush()
    Outbox.update(sent_at=None).execute()  # simulates a lost confirmation
    cgw.forwarder.flush()
    assert len(cloud.delivered) == 2  # the backend deduplicates by eventId


def test_events_the_backend_keeps_rejecting_are_discarded_so_they_do_not_block_the_queue(cgw, cloud):
    queue(cgw, 1)
    cloud.reject = True
    for _ in range(MAX_REJECTIONS):
        cgw.forwarder.flush()
    assert cgw.outbox.pending_count() == 0
    assert cgw.forwarder.stats()["discarded"] == 1
    assert "discarded" in Outbox.get().last_error


def test_without_a_cloud_events_just_accumulate(config, clock):
    app = create_app(config, clock=clock)
    with database.connection_context():
        gw = app.extensions["gateway"]
        queue(gw, 2)
        assert gw.forwarder.flush() == 0
        assert gw.forwarder.stats()["cloudConfigured"] is False
        assert gw.outbox.pending_count() == 2


def test_sent_events_are_purged_after_the_retention_period(cgw, cloud, clock):
    queue(cgw, 1)
    cgw.forwarder.flush()
    clock.advance(hours=25)
    queue(cgw, 1)
    cgw.forwarder.flush()
    assert Outbox.select().count() == 1


# --- US75: the full offline story -------------------------------------------------------
def test_access_attempts_made_while_offline_reach_the_cloud_afterwards(cgw, cloud, clock, publisher):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    device = cgw.devices.register("door-1", area_id="pool", capabilities=["rfid"])
    cgw.devices.touch(device)
    Outbox.delete().execute()
    cloud.down = True
    for uid in ("04A1B2C3", "DEADBEEF"):
        cgw.access.on_access("door-1", {"credential": uid, "credentialType": "RFID", "ts": 0})
        clock.advance(seconds=5)
    assert [a.result for a in AccessAttempt.select().order_by(AccessAttempt.id)] == ["GRANTED", "DENIED"]
    cgw.forwarder.flush()
    assert cloud.delivered == []
    cloud.down = False
    clock.advance(minutes=1)
    cgw.forwarder.flush()
    results = [e["payload"]["result"] for e in cloud.delivered if e["kind"] == "access_attempt"]
    assert results == ["GRANTED", "DENIED"]


# --- cloud client -----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


def make_client(response=None, error=None):
    config = Config(service_token="x", sync_base_url="http://cloud:9000/", sync_api_key="secret", gateway_id="gw-7")
    session = FakeSession(response, error)
    return CloudClient(config, session), session


def test_client_posts_the_batch_with_gateway_identity_and_credentials():
    client, session = make_client(FakeResponse(200, {"accepted": ["a"]}))
    assert client.post_events([{"eventId": "a"}, {"eventId": "b"}]) == ["a"]
    method, url, kwargs = session.requests[0]
    assert (method, url) == ("POST", "http://cloud:9000/api/v1/edge/events")
    assert kwargs["headers"]["Authorization"] == "Bearer secret" and kwargs["headers"]["X-Gateway-Id"] == "gw-7"
    assert kwargs["json"]["gatewayId"] == "gw-7" and kwargs["timeout"] == 5.0


def test_client_treats_a_bare_success_as_everything_accepted():
    client, _ = make_client(FakeResponse(202, {}))
    assert client.post_events([{"eventId": "a"}, {"eventId": "b"}]) == ["a", "b"]


@pytest.mark.parametrize("status", [500, 503, 401, 403, 429])
def test_client_transient_failures_mean_retry_later(status):
    client, _ = make_client(FakeResponse(status, {}))
    with pytest.raises(CloudUnavailable):
        client.post_events([{"eventId": "a"}])


def test_client_other_4xx_are_permanent_rejections():
    client, _ = make_client(FakeResponse(422, {"error": "bad"}))
    with pytest.raises(CloudRejected):
        client.post_events([{"eventId": "a"}])


def test_client_network_errors_mean_retry_later():
    client, _ = make_client(error=requests.ConnectionError("down"))
    with pytest.raises(CloudUnavailable):
        client.post_events([{"eventId": "a"}])


def test_client_passes_the_cached_version_when_pulling():
    client, session = make_client(FakeResponse(200, {"version": 3}))
    assert client.fetch_sync(2) == {"version": 3}
    assert session.requests[0][2]["params"] == {"since": 2}
    client.fetch_sync(None)
    assert session.requests[1][2]["params"] == {}


# --- US81: synchronisation ---------------------------------------------------------------
def test_full_snapshot_fills_the_cache(cgw):
    result = cgw.sync.apply(SyncSchema().load(snapshot()))
    assert result["credentials"] == 1 and result["permissions"] == 1
    assert Credential.get_by_id("04A1B2C3").resident_name == "Ana Perez"  # uid normalised
    assert AreaSchedule.get_by_id("pool").closes_at == "22:00"
    assert cgw.sync.version() == 1 and cgw.sync.status()["lastSyncAt"]


def test_snapshot_makes_a_card_work_at_the_door(cgw, publisher):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    cgw.devices.touch(cgw.devices.register("door-1", area_id="pool"))
    cgw.access.on_access("door-1", {"credential": "04A1B2C3", "credentialType": "RFID", "ts": 0})
    assert publisher.commands("door-1", "access_result")[-1]["params"]["result"] == "GRANTED"


def test_incremental_update_changes_one_card_without_touching_the_rest(cgw):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    update = {"version": 2, "full": False, "credentials": [
        {"uid": "04A1B2C3", "residentId": "r1", "residentName": "Ana Perez", "status": "REVOKED"}]}
    cgw.sync.apply(SyncSchema().load(update))
    assert Credential.get_by_id("04A1B2C3").status == "REVOKED"
    assert AccessPermission.select().count() == 1 and cgw.sync.version() == 2


def test_removed_reservation_and_schedule_are_deleted(cgw):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    cgw.sync.apply(SyncSchema().load(
        {"version": 2, "removedPermissionIds": ["res-1"], "removedAreaIds": ["pool"]}))
    assert AccessPermission.select().count() == 0 and AreaSchedule.select().count() == 0


def test_a_reservation_that_changes_is_replaced_not_duplicated(cgw):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    moved = snapshot(version=2, full=False)
    moved["permissions"][0]["areaId"] = "gym"
    cgw.sync.apply(SyncSchema().load(moved))
    assert [p.area_id for p in AccessPermission.select()] == ["gym"]


def test_older_updates_are_ignored(cgw):
    cgw.sync.apply(SyncSchema().load(snapshot(version=5)))
    result = cgw.sync.apply(SyncSchema().load({"version": 3, "credentials": [
        {"uid": "ZZ", "residentId": "r9"}]}))
    assert result["ignored"] is True and Credential.get_or_none(Credential.uid == "ZZ") is None


def test_full_snapshot_removes_what_the_cloud_no_longer_has(cgw):
    Credential.create(uid="OLD1", resident_id="r2", updated_at=cgw.clock.now())
    cgw.sync.apply(SyncSchema().load(snapshot()))
    assert Credential.get_or_none(Credential.uid == "OLD1") is None


def test_cards_registered_offline_survive_a_snapshot_until_the_cloud_knows_them(cgw):
    cgw.credentials.register("LOCAL1", "r5", "Offline Card")
    cgw.sync.apply(SyncSchema().load(snapshot()))
    assert Credential.get_or_none(Credential.uid == "LOCAL1") is not None
    Outbox.update(sent_at=cgw.clock.now()).execute()  # the cloud acknowledged the event
    cgw.sync.apply(SyncSchema().load(snapshot(version=2)))
    assert Credential.get_or_none(Credential.uid == "LOCAL1") is None


def test_pull_applies_what_the_cloud_returns_and_remembers_the_version(cgw, cloud):
    cloud.snapshot = snapshot(version=7)
    assert cgw.sync.pull() is True
    assert cgw.sync.version() == 7
    cloud.snapshot = snapshot(version=8, full=False, credentials=[], permissions=[], areaSchedules=[])
    cgw.sync.pull()
    assert cloud.sync_requests == [None, 7]


def test_pull_failure_keeps_the_existing_cache_working(cgw, cloud):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    cloud.down = True
    assert cgw.sync.pull() is False
    assert Credential.select().count() == 1


def test_invalid_schedule_time_is_rejected():
    from marshmallow import ValidationError

    with pytest.raises(ValidationError):
        SyncSchema().load({"version": 1, "areaSchedules": [{"areaId": "x", "opensAt": "25:00", "closesAt": "10:00"}]})


def test_stale_cache_warns_once_keeps_working_and_clears_after_a_sync(cgw, clock, publisher):
    cgw.sync.apply(SyncSchema().load(snapshot()))
    clock.advance(hours=25)
    for _ in range(3):
        cgw.sync.check_staleness()
    alerts = [json.loads(e.payload) for e in Outbox.select() if e.kind == "alert"]
    assert [a["alert"] for a in alerts] == ["stale_cache"]
    assert cgw.sync.status()["stale"] is True
    cgw.sync.apply(SyncSchema().load(snapshot(version=2)))
    assert cgw.sync.status()["stale"] is False
    assert [json.loads(e.payload)["state"] for e in Outbox.select() if e.kind == "alert"] == ["raised", "cleared"]


def test_never_synchronised_gateway_is_not_reported_as_stale(cgw):
    assert cgw.sync.is_stale() is False


# --- REST: push, status, outbox ------------------------------------------------------------
def test_api_accepts_a_pushed_update_from_the_cloud(client):
    assert client.post("/api/v1/sync", json=snapshot()).status_code == 401
    response = client.post("/api/v1/sync", json=snapshot(), headers=AUTH)
    assert response.status_code == 200 and response.get_json()["credentials"] == 1
    assert client.post("/api/v1/sync", json={"credentials": []}, headers=AUTH).status_code == 422


def test_api_status_summarises_the_gateway_for_local_operation(client, gw):
    gw.devices.register("d1")
    body = client.get("/api/v1/status", headers=AUTH).get_json()
    assert body["gatewayId"] == "edge-gateway-01"
    assert body["outbox"]["pending"] == 1 and body["outbox"]["cloudConfigured"] is False
    assert body["devices"] == {"INACTIVE": 1} and body["cache"]["version"] is None


def test_api_lists_pending_events_oldest_first(client, gw):
    queue(gw, 3)
    rows = client.get("/api/v1/outbox?limit=2", headers=AUTH).get_json()
    assert [r["payload"]["n"] for r in rows] == [0, 1]
    assert client.get("/api/v1/outbox").status_code == 401


def test_health_reports_outbox_and_cache(client):
    checks = client.get("/health").get_json()["checks"]
    assert checks["outbox"]["pending"] == 0 and checks["cache"]["stale"] is False
