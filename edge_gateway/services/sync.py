"""Keeps the local cache of credentials, reservations and schedules in line with the cloud."""

import logging
from datetime import timedelta

from marshmallow import EXCLUDE, Schema, fields, validate

from ..clock import from_iso, to_iso
from ..db import database
from ..fields import IsoDateTime
from ..models import (
    AccessPermission,
    AreaSchedule,
    Credential,
    Outbox,
    SyncState,
)
from . import outbox as ob
from .access import normalize_uid
from .cloud import CloudRejected, CloudUnavailable

logger = logging.getLogger(__name__)

HHMM = validate.Regexp(r"^([01]\d|2[0-3]):[0-5]\d$", error="Expected HH:MM")


class _Item(Schema):
    class Meta:
        unknown = EXCLUDE


class CredentialItem(_Item):
    uid = fields.String(required=True, validate=validate.Length(min=1, max=128))
    type = fields.String(load_default="RFID", validate=validate.OneOf(["RFID", "MOBILE"]))
    residentId = fields.String(required=True)
    residentName = fields.String(load_default="")
    status = fields.String(load_default="ACTIVE", validate=validate.OneOf(["ACTIVE", "SUSPENDED", "REVOKED"]))
    validFrom = IsoDateTime(load_default=None, allow_none=True)
    validUntil = IsoDateTime(load_default=None, allow_none=True)


class PermissionItem(_Item):
    id = fields.String(required=True)
    residentId = fields.String(required=True)
    areaId = fields.String(required=True)
    startsAt = IsoDateTime(required=True)
    endsAt = IsoDateTime(required=True)


class ScheduleItem(_Item):
    areaId = fields.String(required=True)
    opensAt = fields.String(required=True, validate=HHMM)
    closesAt = fields.String(required=True, validate=HHMM)


class SyncSchema(_Item):
    """Snapshot (`full: true`) or incremental update pushed or pulled from the cloud."""

    version = fields.Integer(required=True, validate=validate.Range(min=0))
    full = fields.Boolean(load_default=False)
    credentials = fields.List(fields.Nested(CredentialItem), load_default=list)
    permissions = fields.List(fields.Nested(PermissionItem), load_default=list)
    removedPermissionIds = fields.List(fields.String(), load_default=list)
    areaSchedules = fields.List(fields.Nested(ScheduleItem), load_default=list)
    removedAreaIds = fields.List(fields.String(), load_default=list)


class SyncService:
    def __init__(self, config, clock, outbox, cloud, alerts):
        self.config = config
        self.clock = clock
        self.outbox = outbox
        self.cloud = cloud
        self.alerts = alerts

    # --- state --------------------------------------------------------------------
    @staticmethod
    def _get(key: str):
        row = SyncState.get_or_none(SyncState.key == key)
        return row.value if row else None

    @staticmethod
    def _set(key: str, value) -> None:
        SyncState.replace(key=key, value=str(value)).execute()

    def version(self) -> int | None:
        value = self._get("version")
        return int(value) if value is not None else None

    def last_sync_at(self):
        return from_iso(self._get("last_sync_at"))

    def is_stale(self) -> bool:
        last = self.last_sync_at()
        if last is None:
            return False
        return self.clock.now() - last > timedelta(seconds=self.config.cache_max_age_s)

    def status(self) -> dict:
        return {
            "version": self.version(),
            "lastSyncAt": to_iso(self.last_sync_at()),
            "stale": self.is_stale(),
        }

    # --- apply --------------------------------------------------------------------
    def apply(self, data: dict) -> dict:
        """Apply a validated snapshot or update in one transaction."""
        current = self.version()
        if not data["full"] and current is not None and data["version"] < current:
            logger.info("Ignoring stale sync update (%s < %s)", data["version"], current)
            return {"ignored": True, "version": current}

        counts = {"credentials": 0, "permissions": 0, "areaSchedules": 0, "removed": 0}
        now = self.clock.now()
        with database.atomic():
            for item in data["credentials"]:
                Credential.replace(
                    uid=normalize_uid(item["uid"]),
                    credential_type=item["type"],
                    resident_id=item["residentId"],
                    resident_name=item["residentName"],
                    status=item["status"],
                    valid_from=item["validFrom"],
                    valid_until=item["validUntil"],
                    updated_at=now,
                ).execute()
                counts["credentials"] += 1
            for item in data["permissions"]:
                AccessPermission.delete().where(AccessPermission.source_id == item["id"]).execute()
                AccessPermission.create(
                    source_id=item["id"],
                    resident_id=item["residentId"],
                    area_id=item["areaId"],
                    starts_at=item["startsAt"],
                    ends_at=item["endsAt"],
                )
                counts["permissions"] += 1
            for item in data["areaSchedules"]:
                AreaSchedule.replace(
                    area_id=item["areaId"], opens_at=item["opensAt"], closes_at=item["closesAt"]
                ).execute()
                counts["areaSchedules"] += 1
            if data["removedPermissionIds"]:
                counts["removed"] += (
                    AccessPermission.delete()
                    .where(AccessPermission.source_id.in_(data["removedPermissionIds"]))
                    .execute()
                )
            if data["removedAreaIds"]:
                counts["removed"] += (
                    AreaSchedule.delete().where(AreaSchedule.area_id.in_(data["removedAreaIds"])).execute()
                )
            if data["full"]:
                counts["removed"] += self._drop_missing(data)
            self._set("version", data["version"])
            self._set("last_sync_at", to_iso(now))
        self.alerts.clear_alert(self.config.gateway_id, "stale_cache")
        logger.info("Cache synchronised to version %s: %s", data["version"], counts)
        return {"version": data["version"], **counts}

    def _drop_missing(self, data: dict) -> int:
        """A full snapshot is the cloud's truth. Cards registered here and not yet reported stay."""
        keep_uids = {normalize_uid(c["uid"]) for c in data["credentials"]}
        unreported = self._unreported_local_uids()
        removed = (
            Credential.delete()
            .where(Credential.uid.not_in(list(keep_uids | unreported)))
            .execute()
        )
        keep_permissions = {p["id"] for p in data["permissions"]}
        removed += AccessPermission.delete().where(AccessPermission.source_id.not_in(list(keep_permissions))).execute()
        keep_areas = {s["areaId"] for s in data["areaSchedules"]}
        removed += AreaSchedule.delete().where(AreaSchedule.area_id.not_in(list(keep_areas))).execute()
        return removed

    @staticmethod
    def _unreported_local_uids() -> set[str]:
        import json

        pending = Outbox.select().where(Outbox.kind == ob.CREDENTIAL_CHANGED, Outbox.sent_at.is_null())
        return {json.loads(row.payload)["uid"] for row in pending}

    # --- pull ---------------------------------------------------------------------
    def pull(self) -> bool:
        """Ask the cloud for what changed since the cached version. Returns True on success."""
        if self.cloud is None:
            return False
        try:
            raw = self.cloud.fetch_sync(self.version())
        except (CloudUnavailable, CloudRejected) as exc:
            logger.warning("Could not synchronise with the cloud: %s", exc)
            return False
        data = SyncSchema().load(raw)
        self.apply(data)
        return True

    def check_staleness(self) -> None:
        """Warn once when the cache has not been refreshed for too long; the gateway keeps working."""
        if self.is_stale():
            self.alerts.raise_alert(
                self.config.gateway_id,
                "stale_cache",
                {"lastSyncAt": to_iso(self.last_sync_at()), "message": "Operating with an outdated cache"},
            )
