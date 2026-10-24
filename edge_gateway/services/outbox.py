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


MAX_REJECTIONS = 5
MAX_BATCHES_PER_FLUSH = 10
MAX_BACKOFF_S = 60


class OutboxForwarder:
    """Delivers queued events to the cloud in chronological order.

    Events stay in the queue until the backend confirms them, so an outage never loses
    data; delivery is at-least-once and the backend deduplicates by eventId.
    """

    def __init__(self, config, clock, outbox, cloud):
        self.config = config
        self.clock = clock
        self.outbox = outbox
        self.cloud = cloud
        self.consecutive_failures = 0
        self.next_attempt_at = None
        self.last_success_at = None
        self.last_error: str | None = None
        self.discarded = 0

    def flush(self) -> int:
        """Send pending events. Returns how many were delivered."""
        from datetime import timedelta

        from .transport import CloudRejected, CloudUnavailable

        if self.cloud is None:
            return 0
        now = self.clock.now()
        if self.next_attempt_at and now < self.next_attempt_at:
            return 0

        delivered = 0
        for _ in range(MAX_BATCHES_PER_FLUSH):
            batch = list(
                Outbox.select()
                .where(Outbox.sent_at.is_null())
                .order_by(Outbox.id)
                .limit(self.config.outbox_batch_size)
            )
            if not batch:
                break
            try:
                accepted = set(self.cloud.post_events([self.outbox.to_wire(e) for e in batch]))
            except CloudUnavailable as exc:
                self._register_failure(batch, str(exc), now)
                self.next_attempt_at = now + timedelta(
                    seconds=min(2 ** self.consecutive_failures, MAX_BACKOFF_S)
                )
                return delivered
            except CloudRejected as exc:
                self._register_rejection(batch, str(exc), now)
                return delivered
            ok_ids = [e.id for e in batch if e.event_id in accepted]
            if ok_ids:
                Outbox.update(sent_at=now, last_error=None).where(Outbox.id.in_(ok_ids)).execute()
            delivered += len(ok_ids)
            self.consecutive_failures = 0
            self.next_attempt_at = None
            self.last_success_at = now
            self.last_error = None
            if len(ok_ids) < len(batch):  # backend refused part of the batch: stop, retry later
                break
        self._purge(now)
        return delivered

    def _register_failure(self, batch, error, now) -> None:
        self.consecutive_failures += 1
        self.last_error = error
        Outbox.update(attempts=Outbox.attempts + 1, last_error=error).where(
            Outbox.id.in_([e.id for e in batch])
        ).execute()
        logger.warning("Cloud unreachable (%s); %d events kept", error, len(batch))

    def _register_rejection(self, batch, error, now) -> None:
        """Events the backend keeps refusing are dropped after a few attempts so they cannot block the queue."""
        self.last_error = error
        Outbox.update(attempts=Outbox.attempts + 1, last_error=error).where(
            Outbox.id.in_([e.id for e in batch])
        ).execute()
        dropped = (
            Outbox.update(sent_at=now, last_error="discarded: " + error)
            .where(Outbox.id.in_([e.id for e in batch]), Outbox.attempts >= MAX_REJECTIONS)
            .execute()
        )
        self.discarded += dropped
        logger.error("Cloud rejected a batch (%s); %d events discarded", error, dropped)

    def _purge(self, now) -> None:
        from datetime import timedelta

        limit = now - timedelta(seconds=self.config.outbox_retention_s)
        Outbox.delete().where(Outbox.sent_at.is_null(False), Outbox.sent_at < limit).execute()

    def stats(self) -> dict:
        from ..clock import to_iso

        oldest = (
            Outbox.select(Outbox.occurred_at)
            .where(Outbox.sent_at.is_null())
            .order_by(Outbox.id)
            .first()
        )
        return {
            "cloudConfigured": self.cloud is not None,
            "pending": self.outbox.pending_count(),
            "oldestPendingAt": to_iso(oldest.occurred_at) if oldest else None,
            "lastSuccessAt": to_iso(self.last_success_at),
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
            "discarded": self.discarded,
        }
