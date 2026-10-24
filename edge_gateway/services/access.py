"""Access decisions taken locally from the cached cloud state (works without internet)."""

import logging
from datetime import time, timedelta

from ..clock import local_time
from ..db import database
from ..models import (
    AccessAttempt,
    AccessPermission,
    AreaSchedule,
    Credential,
    Device,
    DoorReservation,
)
from . import outbox as ob

logger = logging.getLogger(__name__)

GRANTED = "GRANTED"
DENIED = "DENIED"

MESSAGES = {
    "ok": "Access granted",
    "unknown_credential": "Card not registered",
    "credential_suspended": "Card suspended",
    "credential_revoked": "Card revoked",
    "credential_expired": "Card expired",
    "outside_schedule": "Outside schedule",
    "no_permission": "No reservation",
    "not_authorized": "Not authorized",
    "maintenance": "Under maintenance",
    "unsupported_credential_type": "Unsupported card",
}

REPEATED_DENIALS = 3
REPEATED_DENIALS_WINDOW = timedelta(minutes=5)


def normalize_uid(value: str) -> str:
    return value.strip().upper()


def mask(uid: str) -> str:
    """Show only the last four characters of a credential."""
    return "*" * max(len(uid) - 4, 0) + uid[-4:]


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def within_schedule(schedule: AreaSchedule, local_now) -> bool:
    opens, closes = _parse_hhmm(schedule.opens_at), _parse_hhmm(schedule.closes_at)
    current = local_now.time().replace(second=0, microsecond=0)
    if opens <= closes:
        return opens <= current < closes
    return current >= opens or current < closes  # overnight window


class AccessService:
    def __init__(self, config, clock, devices, commands, outbox):
        self.config = config
        self.clock = clock
        self.devices = devices
        self.commands = commands
        self.outbox = outbox
        self._recent: dict[tuple[str, str], object] = {}
        self._mode_sent_at: dict[str, object] = {}

    def on_access(self, device_id: str, data: dict) -> None:
        device = self.devices.known_device(device_id)
        if device is None:
            return
        self.devices.touch(device)

        now = self.clock.now()
        uid = normalize_uid(data["credential"])
        if self._is_repeated_read(device_id, uid, now):
            logger.debug("Ignoring repeated read of %s on %s", mask(uid), device_id)
            return

        credential = None
        if device.status == "MAINTENANCE":
            result, reason = DENIED, "maintenance"
        elif data["credentialType"] != "RFID":
            result, reason = DENIED, "unsupported_credential_type"
        else:
            result, reason, credential = self.decide(device, uid, now)

        with database.atomic():  # the attempt and its event are stored together or not at all
            AccessAttempt.create(
                device_id=device_id,
                credential=uid,
                resident_id=credential.resident_id if credential else None,
                result=result,
                reason=reason,
                occurred_at=now,
            )
            self.outbox.enqueue(
                ob.ACCESS_ATTEMPT,
                {
                    "deviceId": device_id,
                    "areaId": device.area_id,
                    "credential": uid,
                    "credentialType": data["credentialType"],
                    "residentId": credential.resident_id if credential else None,
                    "result": result,
                    "reason": reason,
                    "accessMode": self.current_mode(device, now),
                },
                occurred_at=now,
            )
        self._detect_repeated_denials(device_id, uid, now)
        self._answer_node(device, result, reason, credential)

    def current_mode(self, device, now=None) -> str:
        now = now or self.clock.now()
        has_res = (
            DoorReservation.select()
            .where(
                DoorReservation.device_id == device.device_id,
                DoorReservation.starts_at <= now,
                DoorReservation.ends_at > now,
            )
            .exists()
        )
        return "RESERVED" if has_res else "FREE_ACCESS"

    def resend_mode(self, device: Device) -> None:
        now = self.clock.now()
        mode = self.current_mode(device, now)
        if mode == "RESERVED":
            params = {"mode": "RESERVED"}
            if device.led_color is not None:
                params["color"] = device.led_color
        else:
            params = {"mode": "FREE_ACCESS"}
        try:
            self.commands.send(device.device_id, "mode", params)
            self._mode_sent_at[device.device_id] = now
        except Exception:
            logger.exception("Could not resend mode to node %s", device.device_id)

    def reconcile_mode(self, device: Device, reported_mode: str | None, reported_color: str | None) -> bool:
        """Re-send `mode` when the state a node reports differs from the latest configuration.

        Covers a rebooted node, an MQTT reconnection and a command lost while disconnected.
        Nodes that do not report their state (older firmware) are left alone. Returns True if sent.
        """
        if reported_mode is None:
            return False
        now = self.clock.now()
        desired_mode = self.current_mode(device, now)
        stale = reported_mode != desired_mode or (
            desired_mode == "RESERVED"
            and device.led_color is not None
            and (reported_color or "").upper() != device.led_color.upper()
        )
        if not stale:
            return False
        last = self._mode_sent_at.get(device.device_id)
        if last is not None and now - last < timedelta(seconds=self.config.mode_resync_interval_s):
            return False  # the previous re-send is still on its way; the next heartbeat re-checks
        logger.info(
            "Node %s reports %s/%s, expected %s/%s: re-sending mode",
            device.device_id, reported_mode, reported_color, desired_mode, device.led_color,
        )
        self.resend_mode(device)
        return True

    def check_mode_changes(self, now=None) -> list[str]:
        now = now or self.clock.now()
        changed = []
        for device in Device.select():
            if "lock" not in device.capability_list:
                continue
            calculated = self.current_mode(device, now)
            if calculated != device.current_mode:
                old_mode = device.current_mode
                device.current_mode = calculated
                if calculated == "FREE_ACCESS":
                    device.led_color = None
                device.save()

                if calculated == "RESERVED":
                    params = {"mode": "RESERVED"}
                    if device.led_color is not None:
                        params["color"] = device.led_color
                else:
                    params = {"mode": "FREE_ACCESS"}

                self.commands.send(device.device_id, "mode", params)

                self.outbox.enqueue(
                    ob.DEVICE_STATUS,
                    {
                        "deviceId": device.device_id,
                        "event": "mode_changed",
                        "from": old_mode,
                        "to": calculated,
                    },
                    occurred_at=now,
                )
                changed.append(device.device_id)
        return changed

    def decide(self, device, uid: str, now):
        """Return (result, reason, credential) for a card read at a node."""
        credential = Credential.get_or_none(Credential.uid == uid)
        if credential is None:
            return DENIED, "unknown_credential", None
        if credential.status != "ACTIVE":
            return DENIED, f"credential_{credential.status.lower()}", credential
        if (credential.valid_from and now < credential.valid_from) or (
            credential.valid_until and now > credential.valid_until
        ):
            return DENIED, "credential_expired", credential

        mode = self.current_mode(device, now)
        if device.managed == "cloud":
            if mode == "RESERVED":
                permitted = (
                    AccessPermission.select()
                    .where(
                        AccessPermission.device_id == device.device_id,
                        AccessPermission.resident_id == credential.resident_id,
                        AccessPermission.starts_at <= now,
                        AccessPermission.ends_at >= now,
                    )
                    .exists()
                )
                if not permitted:
                    return DENIED, "not_authorized", credential
            return GRANTED, "ok", credential

        if device.area_id:
            schedule = AreaSchedule.get_or_none(AreaSchedule.area_id == device.area_id)
            if schedule and not within_schedule(schedule, local_time(now, self.config.timezone)):
                return DENIED, "outside_schedule", credential
            permitted = (
                AccessPermission.select()
                .where(
                    AccessPermission.resident_id == credential.resident_id,
                    AccessPermission.area_id == device.area_id,
                    AccessPermission.starts_at <= now,
                    AccessPermission.ends_at >= now,
                )
                .exists()
            )
            if not permitted:
                return DENIED, "no_permission", credential
        return GRANTED, "ok", credential

    # --- helpers ------------------------------------------------------------------
    def _is_repeated_read(self, device_id: str, uid: str, now) -> bool:
        key = (device_id, uid)
        last = self._recent.get(key)
        self._recent[key] = now
        window = timedelta(seconds=self.config.dedupe_window_s)
        return last is not None and now - last < window

    def _detect_repeated_denials(self, device_id: str, uid: str, now) -> None:
        recent = list(
            AccessAttempt.select()
            .where(
                AccessAttempt.device_id == device_id,
                AccessAttempt.credential == uid,
                AccessAttempt.occurred_at >= now - REPEATED_DENIALS_WINDOW,
            )
            .order_by(AccessAttempt.id.desc())
            .limit(REPEATED_DENIALS + 1)
        )
        streak = recent[:REPEATED_DENIALS]
        reached = len(streak) == REPEATED_DENIALS and all(a.result == DENIED for a in streak)
        if reached and not (len(recent) > REPEATED_DENIALS and recent[-1].result == DENIED):
            self.outbox.enqueue(
                ob.ALERT,
                {
                    "alert": "possible_unauthorized_access",
                    "state": "raised",
                    "deviceId": device_id,
                    "credential": uid,
                    "attempts": REPEATED_DENIALS,
                },
            )

    def _answer_node(self, device, result: str, reason: str, credential) -> None:
        """Tell the node what to do: unlock, show a message and play a sound."""
        message = MESSAGES.get(reason, "Access denied")
        params = {
            "result": result,
            "reason": reason,
            "message": message,
            "buzzer": "granted" if result == GRANTED else "denied",
        }
        if result == GRANTED:
            params["name"] = credential.resident_name
            params["lockMs"] = self.config.lock_open_ms
        try:
            self.commands.send(device.device_id, "access_result", params)
        except Exception:
            logger.exception("Could not answer node %s (decision already stored)", device.device_id)
