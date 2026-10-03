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
