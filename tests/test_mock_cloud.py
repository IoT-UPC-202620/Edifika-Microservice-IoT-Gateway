import pytest

from mock_cloud import app as mock


@pytest.fixture
def cloud():
    mock._events.clear()
    mock._seen.clear()
    mock._state.update(down=False, version=1)
    mock._credentials.clear()
    mock._credentials["04A1B2C3"] = {
        "uid": "04A1B2C3", "type": "RFID", "residentId": "r-001",
        "residentName": "Ana Perez", "status": "ACTIVE", "v": 1,
    }
    return mock.app.test_client()


AUTH = {"Authorization": f"Bearer {mock.TOKEN}"}


def test_the_backend_requires_the_gateway_token(cloud):
    assert cloud.get("/api/v1/edge/sync").status_code == 401
    assert cloud.post("/api/v1/edge/events", json={"events": []}).status_code == 401


def test_events_are_deduplicated_by_event_id(cloud):
    body = {"gatewayId": "gw", "events": [{"eventId": "a", "kind": "reading"}, {"eventId": "b", "kind": "alert"}]}
    first = cloud.post("/api/v1/edge/events", json=body, headers=AUTH)
    cloud.post("/api/v1/edge/events", json=body, headers=AUTH)
    assert first.status_code == 202 and first.get_json()["accepted"] == ["a", "b"]
    assert cloud.get("/events").get_json()["count"] == 2


def test_malformed_event_batches_are_rejected(cloud):
    assert cloud.post("/api/v1/edge/events", json={"events": [{"kind": "x"}]}, headers=AUTH).status_code == 422


def test_first_sync_is_a_full_snapshot_and_later_ones_only_send_changes(cloud):
    full = cloud.get("/api/v1/edge/sync", headers=AUTH).get_json()
    assert full["full"] is True and [c["uid"] for c in full["credentials"]] == ["04A1B2C3"]
    assert full["permissions"][0]["areaId"] == "pool"
    unchanged = cloud.get("/api/v1/edge/sync?since=1", headers=AUTH).get_json()
    assert unchanged["full"] is False and unchanged["credentials"] == []
    cloud.post("/admin/credentials", json={"uid": "aa11", "residentId": "r2", "status": "REVOKED"})
    changed = cloud.get("/api/v1/edge/sync?since=1", headers=AUTH).get_json()
    assert [c["uid"] for c in changed["credentials"]] == ["AA11"] and changed["version"] == 2


def test_simulated_outage_makes_the_api_unavailable(cloud):
    cloud.post("/admin/outage", json={"down": True})
    assert cloud.get("/api/v1/edge/sync", headers=AUTH).status_code == 503
    cloud.post("/admin/outage", json={"down": False})
    assert cloud.get("/api/v1/edge/sync", headers=AUTH).status_code == 200
