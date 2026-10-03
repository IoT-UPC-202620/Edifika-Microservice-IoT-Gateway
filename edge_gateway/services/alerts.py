"""Alert state: each condition is announced once when raised and once when cleared."""

from peewee import IntegrityError

from ..db import database
from ..models import ActiveAlert
from . import outbox as ob


class AlertService:
    def __init__(self, clock, outbox):
        self.clock = clock
        self.outbox = outbox

    def is_active(self, device_id: str, kind: str) -> bool:
        return ActiveAlert.get_or_none(
            (ActiveAlert.device_id == device_id) & (ActiveAlert.kind == kind)
        ) is not None

    def raise_alert(self, device_id: str, kind: str, details: dict | None = None) -> bool:
        """Raise an alert. Returns False when it was already active."""
        try:
            with database.atomic():
                ActiveAlert.create(device_id=device_id, kind=kind, raised_at=self.clock.now())
                self.outbox.enqueue(
                    ob.ALERT,
                    {"alert": kind, "state": "raised", "deviceId": device_id, **(details or {})},
                )
        except IntegrityError:
            return False
        return True

    def clear_alert(self, device_id: str, kind: str, details: dict | None = None) -> bool:
        """Clear an alert. Returns False when it was not active."""
        with database.atomic():
            removed = (
                ActiveAlert.delete()
                .where((ActiveAlert.device_id == device_id) & (ActiveAlert.kind == kind))
                .execute()
            )
            if removed:
                self.outbox.enqueue(
                    ob.ALERT,
                    {"alert": kind, "state": "cleared", "deviceId": device_id, **(details or {})},
                )
        return bool(removed)
