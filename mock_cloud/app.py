"""Stand-in for the Edifika backend, used to run the whole stack locally.

It implements the two endpoints the Edge Gateway consumes (see the README, section
"Edge <-> Backend contract"), keeps what it receives in memory and lets you simulate
an outage or issue and revoke credentials.
"""

import os
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

TOKEN = os.environ.get("MOCK_CLOUD_TOKEN", "dev-cloud-token")
POOL_OPENS = os.environ.get("MOCK_POOL_OPENS", "00:00")
POOL_CLOSES = os.environ.get("MOCK_POOL_CLOSES", "23:59")

_lock = threading.Lock()
_state = {"down": False, "version": 1}
_events: list[dict] = []
_seen: set[str] = set()
_credentials: dict[str, dict] = {
    "04A1B2C3": {
        "uid": "04A1B2C3", "type": "RFID", "residentId": "r-001",
        "residentName": "Ana Perez", "status": "ACTIVE", "v": 1,
    }
}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _authorized() -> bool:
    return request.headers.get("Authorization", "") == f"Bearer {TOKEN}"


@app.before_request
def _guard():
    if request.path.startswith("/api/") and not _authorized():
        return jsonify(message="Missing or invalid bearer token"), 401
    if request.path.startswith("/api/") and _state["down"]:
        return jsonify(message="Simulated outage"), 503
    return None


@app.post("/api/v1/edge/events")
def receive_events():
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list) or not all(isinstance(e, dict) and e.get("eventId") for e in events):
        return jsonify(message="events must be a list of objects with an eventId"), 422
    with _lock:
        for event in events:
            if event["eventId"] not in _seen:  # idempotent by eventId
                _seen.add(event["eventId"])
                _events.append({**event, "gatewayId": body.get("gatewayId")})
    return jsonify(accepted=[e["eventId"] for e in events]), 202


@app.get("/api/v1/edge/sync")
def sync():
    since = request.args.get("since", type=int)
    now = datetime.now(timezone.utc)
    with _lock:
        credentials = [
            {k: v for k, v in c.items() if k != "v"}
            for c in _credentials.values()
            if since is None or c["v"] > since
        ]
        version = _state["version"]
    return jsonify(
        version=version,
        full=since is None,
        credentials=credentials,
        permissions=[
            {
                "id": "res-demo-1", "residentId": "r-001", "areaId": "pool",
                "startsAt": _iso(now - timedelta(hours=1)), "endsAt": _iso(now + timedelta(hours=8)),
            }
        ],
        areaSchedules=[{"areaId": "pool", "opensAt": POOL_OPENS, "closesAt": POOL_CLOSES}],
    )


# --- helpers for demos (no authentication) ---------------------------------------------
@app.get("/events")
def list_events():
    kind = request.args.get("kind")
    with _lock:
        rows = [e for e in _events if kind is None or e["kind"] == kind]
    return jsonify(count=len(rows), events=rows[-int(request.args.get("limit", 50)):])


@app.post("/admin/outage")
def outage():
    _state["down"] = bool((request.get_json(silent=True) or {}).get("down", True))
    return jsonify(down=_state["down"])


@app.post("/admin/credentials")
def upsert_credential():
    body = request.get_json(silent=True) or {}
    if not body.get("uid") or not body.get("residentId"):
        return jsonify(message="uid and residentId are required"), 422
    with _lock:
        _state["version"] += 1
        _credentials[body["uid"].upper()] = {
            "uid": body["uid"].upper(), "type": body.get("type", "RFID"),
            "residentId": body["residentId"], "residentName": body.get("residentName", ""),
            "status": body.get("status", "ACTIVE"), "v": _state["version"],
        }
        return jsonify(version=_state["version"])


@app.get("/health")
def health():
    return jsonify(status="ok", down=_state["down"], events=len(_events))
