"""Local administration of credentials (manual registration and blocking, also offline)."""

from ..db import database
from ..models import Credential
from . import outbox as ob
from .access import normalize_uid
from .errors import ConflictError, NotFoundError, ValidationFailed

STATUSES = ("ACTIVE", "SUSPENDED", "REVOKED")


class CredentialService:
    def __init__(self, clock, outbox):
        self.clock = clock
        self.outbox = outbox

    def register(self, uid, resident_id, resident_name="", credential_type="RFID",
                 valid_from=None, valid_until=None) -> Credential:
        """Register a card by hand. The cloud learns about it through the outbox."""
        uid = normalize_uid(uid)
        if valid_from and valid_until and valid_from >= valid_until:
            raise ValidationFailed("validFrom must be earlier than validUntil")
        existing = Credential.get_or_none(Credential.uid == uid)
        if existing and existing.resident_id != resident_id:
            raise ConflictError("This card is already in use")
        with database.atomic():
            Credential.replace(
                uid=uid,
                credential_type=credential_type,
                resident_id=resident_id,
                resident_name=resident_name,
                status="ACTIVE",
                valid_from=valid_from,
                valid_until=valid_until,
                updated_at=self.clock.now(),
            ).execute()
            self.outbox.enqueue(
                ob.CREDENTIAL_CHANGED,
                {"uid": uid, "residentId": resident_id, "status": "ACTIVE", "source": "edge_manual"},
            )
        return Credential.get_by_id(uid)

    def set_status(self, uid: str, status: str) -> Credential:
        if status not in STATUSES:
            raise ValidationFailed(f"Status must be one of {', '.join(STATUSES)}")
        credential = Credential.get_or_none(Credential.uid == normalize_uid(uid))
        if credential is None:
            raise NotFoundError("Credential not found")
        with database.atomic():
            credential.status = status
            credential.updated_at = self.clock.now()
            credential.save()
            self.outbox.enqueue(
                ob.CREDENTIAL_CHANGED,
                {"uid": credential.uid, "residentId": credential.resident_id, "status": status, "source": "edge_manual"},
            )
        return credential

    def list_all(self) -> list[Credential]:
        return list(Credential.select().order_by(Credential.uid))
