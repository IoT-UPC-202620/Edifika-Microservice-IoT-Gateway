import json
from datetime import datetime, timedelta


class FakeClock:
    def __init__(self, now=None):
        self._now = now or datetime(2026, 5, 4, 15, 0, 0)  # 10:00 in Lima (UTC-5)

    def now(self):
        return self._now

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)


class FakePublisher:
    """Records commands instead of publishing them to a broker."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def publish_command(self, device_id, command):
        if self.fail:
            raise RuntimeError("broker down")
        self.sent.append((device_id, command))

    def commands(self, device_id=None, type_=None):
        return [
            c
            for d, c in self.sent
            if (device_id is None or d == device_id) and (type_ is None or c["type"] == type_)
        ]


def msg(prefix, device_id, channel, **fields):
    body = {"v": 1, "deviceId": device_id, "ts": 1_780_000_000.0, **fields}
    return f"{prefix}/nodes/{device_id}/{channel}", json.dumps(body)


def firmware_msg(prefix, device_id, channel, **fields):
    body = {"v": "1"}
    if "ts" in fields:
        body["ts"] = fields.pop("ts")
    else:
        body["ts"] = "2026-10-24T15:04:05Z"

    if channel == "heartbeat":
        body["deviceId"] = fields.pop("deviceId", device_id)
    elif "deviceId" in fields:
        body["deviceId"] = fields.pop("deviceId")

    body.update(fields)
    return f"{prefix}/nodes/{device_id}/{channel}", json.dumps(body)


class FakeCloud:
    """Stands in for the backend: records delivered events and serves a sync snapshot."""

    def __init__(self):
        self.delivered = []  # event dicts, in delivery order
        self.down = False
        self.reject = False
        self.accept_only = None  # limit how many events of a batch are accepted
        self.snapshot = None
        self.calls = 0
        self.sync_requests = []

    def post_events(self, events):
        from edge_gateway.services.transport import CloudRejected, CloudUnavailable

        self.calls += 1
        if self.down:
            raise CloudUnavailable("connection refused")
        if self.reject:
            raise CloudRejected("HTTP 422: bad event")
        known = {e["eventId"] for e in self.delivered}
        accepted = events if self.accept_only is None else events[: self.accept_only]
        self.delivered.extend(e for e in accepted if e["eventId"] not in known)
        return [e["eventId"] for e in accepted]

    def fetch_sync(self, since_version):
        from edge_gateway.services.transport import CloudUnavailable

        self.sync_requests.append(since_version)
        if self.down:
            raise CloudUnavailable("connection refused")
        return self.snapshot
