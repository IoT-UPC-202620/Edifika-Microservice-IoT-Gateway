"""Time helpers. All stored timestamps are naive UTC datetimes (ISO 8601 on the wire)."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso(value: datetime | None) -> str | None:
    """Naive UTC datetime -> ISO 8601 string with a Z suffix."""
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds") + "Z"


def from_iso(value: str | None) -> datetime | None:
    """ISO 8601 string (Z or offset) -> naive UTC datetime."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def to_epoch(value: datetime) -> float:
    return value.replace(tzinfo=timezone.utc).timestamp()


def local_time(value: datetime, tz_name: str):
    """Naive UTC datetime -> aware datetime in the configured timezone."""
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
