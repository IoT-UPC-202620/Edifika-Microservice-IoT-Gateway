"""Outbound event queue: the only way data leaves the gateway towards the cloud."""

import json
import logging
import uuid

from ..clock import to_iso
from ..models import Outbox

logger = logging.getLogger(__name__)

# Event kinds
ACCESS_ATTEMPT = "access_attempt"
READING = "reading"
ALERT = "alert"
DEVICE_STATUS = "device_status"
COMMAND_RESULT = "command_result"
CREDENTIAL_CHANGED = "credential_changed"

# Telemetry is the first thing dropped when the queue is full.
DISPOSABLE_KINDS = (READING,)


class OutboxService:
    def __init__(self, config, clock):
        self.config = config
        self.clock = clock

    def enqueue(self, kind: str, payload: dict, occurred_at=None) -> Outbox:
        """Queue an event. Safe to call inside an outer transaction."""
        now = self.clock.now()
        occurred_at = occurred_at or now
        event = Outbox.create(
            event_id=uuid.uuid4().hex,
            kind=kind,
            payload=json.dumps(payload, separators=(",", ":")),
            occurred_at=occurred_at,
            created_at=now,
        )
        self._enforce_limit()
        return event

    def pending_count(self) -> int:
        return Outbox.select().where(Outbox.sent_at.is_null()).count()

    def _enforce_limit(self) -> None:
        overflow = self.pending_count() - self.config.outbox_max_rows
        if overflow <= 0:
            return
        victims = [
            row.id
            for row in Outbox.select(Outbox.id)
            .where(Outbox.sent_at.is_null(), Outbox.kind.in_(DISPOSABLE_KINDS))
            .order_by(Outbox.id)
            .limit(overflow)
        ]
        if victims:
            Outbox.delete().where(Outbox.id.in_(victims)).execute()
        logger.warning(
            "Outbox over capacity: dropped %d oldest telemetry events", len(victims)
        )

    @staticmethod
    def to_wire(event: Outbox) -> dict:
        return {
            "eventId": event.event_id,
            "kind": event.kind,
            "occurredAt": to_iso(event.occurred_at),
            "payload": json.loads(event.payload),
        }
