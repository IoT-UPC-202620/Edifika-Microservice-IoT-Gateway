"""Node registry, liveness tracking and clock synchronisation."""

import json
import logging
from datetime import timedelta

from ..clock import from_epoch, to_epoch, to_iso
from ..models import Device, DiscoveredDevice
from . import outbox as ob
from .errors import ConflictError, NotFoundError, ValidationFailed

logger = logging.getLogger(__name__)

CAPABILITIES = {
    "rfid",
    "lock",
    "buzzer",
    "oled",
    "humidity",
    "ultrasonic",
    "rtc",
    "rgb_led",
    "temperature",
}
TIME_SYNC_COOLDOWN_S = 30
# A pending node is re-reported at least this often while it keeps announcing itself,
# so the cloud can tell a board that is still there from one that was unplugged.
DISCOVERY_REPORT_INTERVAL_S = 300

ACTIVE = "ACTIVE"
INACTIVE = "INACTIVE"
OFFLINE = "OFFLINE"
MAINTENANCE = "MAINTENANCE"


class DeviceService:
    def __init__(self, config, clock, commands, outbox, alerts, access=None):
        self.config = config
        self.clock = clock
        self.commands = commands
        self.outbox = outbox
        self.alerts = alerts
        self.access = access
        self._last_time_sync: dict[str, object] = {}

    # --- registry -----------------------------------------------------------------
    def register(self, device_id: str, area_id=None, location="", capabilities=()) -> Device:
        unknown = set(capabilities) - CAPABILITIES
        if unknown:
            raise ValidationFailed(f"Unknown capabilities: {', '.join(sorted(unknown))}")
        if Device.get_or_none(Device.device_id == device_id):
            raise ConflictError("The device is already registered")
        device = Device.create(
            device_id=device_id,
            area_id=area_id,
            location=location,
            capabilities=json.dumps(sorted(set(capabilities))),
        )
        self.outbox.enqueue(
            ob.DEVICE_STATUS,
            {"deviceId": device_id, "status": INACTIVE, "event": "registered", "areaId": area_id},
        )
        return device

    def get(self, device_id: str) -> Device | None:
        return Device.get_or_none(Device.device_id == device_id)

    def require(self, device_id: str) -> Device:
        device = self.get(device_id)
        if device is None:
            raise NotFoundError("Device not found")
        return device

    def update_settings(self, device_id: str, patch: dict) -> Device:
        """Calibrate a node (tank height, thresholds). Only known keys inside their limits are kept."""
        from .telemetry import SETTING_LIMITS

        device = self.require(device_id)
        for key, value in patch.items():
            if key not in SETTING_LIMITS:
                raise ValidationFailed(f"Unknown setting: {key}")
            low, high = SETTING_LIMITS[key]
            if not low <= value <= high:
                raise ValidationFailed(f"{key} must be between {low} and {high}")
        merged = {**device.settings, **patch}
        device.config = json.dumps(merged)
        device.save()
        self.outbox.enqueue(
            ob.DEVICE_STATUS, {"deviceId": device_id, "event": "settings_updated", "settings": merged}
        )
        return device

    def list_all(self) -> list[Device]:
        return list(Device.select().order_by(Device.device_id))

    def known_device(self, device_id: str) -> Device | None:
        """Look up the sender of an MQTT message; unknown senders are ignored.

        A node waiting for adoption is expected to be inert, so its messages are dropped
        quietly; any other unknown sender raises `unregistered_device` once.
        """
        device = self.get(device_id)
        if device is None:
            if DiscoveredDevice.get_or_none(DiscoveredDevice.device_id == device_id) is not None:
                logger.debug("Ignoring message from %s: pending adoption", device_id)
            else:
                logger.warning("Ignoring message from unregistered device %s", device_id)
                self.alerts.raise_alert(device_id, "unregistered_device")
        return device

    # --- discovery ----------------------------------------------------------------
    def discovered(self, device_id: str) -> DiscoveredDevice | None:
        return DiscoveredDevice.get_or_none(DiscoveredDevice.device_id == device_id)

    def on_announce(self, device_id: str, data: dict) -> None:
        """A node introduced itself.

        A registered node gets its configuration back at once (`mode`), which is also its
        signal that it is adopted. An unknown node is kept as pending: it stays inert and
        is reported to the cloud as DeviceDiscovered, in this gateway's building.
        """
        device = self.get(device_id)
        if device is not None:
            DiscoveredDevice.delete().where(DiscoveredDevice.device_id == device_id).execute()
            self._record_firmware(device, data.get("fw"))
            previous_status = device.status
            self.touch(device)  # coming back from OFFLINE/INACTIVE already re-sends `mode`
            already_sent = previous_status in (OFFLINE, INACTIVE)
            if (not already_sent and device.status != MAINTENANCE
                    and "lock" in device.capability_list and self.access is not None):
                self.access.resend_mode(device)
            return

        now = self.clock.now()
        snapshot = {
            "mac": data["mac"].upper(),
            "firmware": data["fw"],
            "model": data.get("model"),
            "capabilities": json.dumps(sorted(set(data["capabilities"]))),
            "ip": data.get("ip"),
            "rssi": data.get("rssi"),
            "pairing_code": data["pairingCode"],
        }
        pending = self.discovered(device_id)
        if pending is None:
            pending = DiscoveredDevice.create(device_id=device_id, first_seen=now, last_seen=now, **snapshot)
            changed = True
            logger.info("Discovered node %s (code %s)", device_id, pending.pairing_code)
        else:
            changed = any(getattr(pending, key) != value for key, value in snapshot.items() if key != "rssi")
            for key, value in snapshot.items():
                setattr(pending, key, value)
            pending.last_seen = now
        due = pending.reported_at is None or now - pending.reported_at >= timedelta(seconds=DISCOVERY_REPORT_INTERVAL_S)
        if changed or due:
            pending.reported_at = now
            self.outbox.enqueue(ob.DEVICE_DISCOVERED, self._discovery_payload(pending), occurred_at=now)
        pending.save()

    def list_discovered(self) -> list[DiscoveredDevice]:
        return list(DiscoveredDevice.select().order_by(DiscoveredDevice.last_seen.desc()))

    @staticmethod
    def _discovery_payload(pending: DiscoveredDevice) -> dict:
        return {
            "deviceCode": pending.device_id,
            "mac": pending.mac,
            "firmware": pending.firmware,
            "model": pending.model,
            "capabilities": pending.capability_list,
            "ip": pending.ip,
            "rssi": pending.rssi,
            "pairingCode": pending.pairing_code,
            "firstSeenAt": to_iso(pending.first_seen),
            "lastSeenAt": to_iso(pending.last_seen),
        }

    # --- liveness -----------------------------------------------------------------
    def touch(self, device: Device) -> Device:
        """Record activity from a node. A node in maintenance stays deactivated."""
        now = self.clock.now()
        device.last_seen = now
        previous_status = device.status
        if device.status != MAINTENANCE:
            device.status = ACTIVE
        device.save()
        if previous_status in (OFFLINE, INACTIVE) and device.status == ACTIVE:
            self.outbox.enqueue(
                ob.DEVICE_STATUS,
                {"deviceId": device.device_id, "event": "online"},
                occurred_at=now,
            )
            # INACTIVE -> ACTIVE is also the first heartbeat after adoption: the node is still
            # waiting for its first `mode` to leave the unregistered state
            if "lock" in device.capability_list and self.access is not None:
                self.access.resend_mode(device)
            if previous_status == INACTIVE:
                DiscoveredDevice.delete().where(DiscoveredDevice.device_id == device.device_id).execute()
        if device.status == ACTIVE:
            self.alerts.clear_alert(device.device_id, "device_offline")
        self.alerts.clear_alert(device.device_id, "firmware_incompatible")
        return device

    def _record_firmware(self, device: Device, fw: str | None) -> None:
        if fw and fw != device.firmware:
            device.firmware = fw
            self.outbox.enqueue(
                ob.DEVICE_STATUS,
                {"deviceId": device.device_id, "event": "firmware", "firmware": fw},
                occurred_at=self.clock.now(),
            )

    def on_heartbeat(self, device_id: str, data: dict) -> None:
        device = self.known_device(device_id)
        if device is None:
            return
        self._record_firmware(device, data.get("fw"))
        self.touch(device)
        if device.status != MAINTENANCE and "lock" in device.capability_list and self.access is not None:
            self.access.reconcile_mode(device, data.get("mode"), data.get("color"))
        if device.status != MAINTENANCE:
            if data.get("ts") is not None:
                self._sync_clock_if_drifted(device, data["ts"])

    def check_offline(self, now=None) -> list[str]:
        """Mark silent nodes OFFLINE. Returns the ids that changed state."""
        now = now or self.clock.now()
        limit = now - timedelta(seconds=self.config.heartbeat_timeout_s)
        silent = list(
            Device.select().where(Device.status == ACTIVE, Device.last_seen < limit)
        )
        for device in silent:
            device.status = OFFLINE
            device.save()
            dropped = self.commands.pending.discard_device(device.device_id)
            self.alerts.raise_alert(
                device.device_id,
                "device_offline",
                {"lastSeen": to_iso(device.last_seen), "discardedCommands": dropped},
            )
            self.outbox.enqueue(
                ob.DEVICE_STATUS,
                {
                    "deviceId": device.device_id,
                    "event": "offline",
                    "lastSeen": to_iso(device.last_seen),
                },
                occurred_at=now,
            )
        return [d.device_id for d in silent]

    # --- maintenance (US89) -------------------------------------------------------
    def set_maintenance(self, device_id: str, enabled: bool, minutes: int | None = None, actor: str = "api") -> Device:
        """Deactivate a node for service work, or return it to operation.

        While in maintenance the node ignores cards, readings and commands and raises no alerts.
        """
        device = self.require(device_id)
        now = self.clock.now()
        if enabled:
            minutes = minutes or 120
            if not 1 <= minutes <= self.config.max_maintenance_minutes:
                raise ValidationFailed(
                    f"Duration must be between 1 and {self.config.max_maintenance_minutes} minutes"
                )
            if device.status != MAINTENANCE:
                device.maintenance_since = now
            device.status = MAINTENANCE
            device.maintenance_until = now + timedelta(minutes=minutes)
            device.save()
            self.commands.pending.discard_device(device_id)
            self._notify_maintenance(device, active=True)
            self.outbox.enqueue(
                ob.DEVICE_STATUS,
                {"deviceId": device_id, "event": "maintenance_started", "by": actor,
                 "until": to_iso(device.maintenance_until)},
            )
            return device

        if device.status != MAINTENANCE:
            raise ConflictError("The device is not in maintenance")
        duration = (now - device.maintenance_since).total_seconds() if device.maintenance_since else 0
        device.maintenance_since = None
        device.maintenance_until = None
        recent = device.last_seen and now - device.last_seen <= timedelta(seconds=self.config.heartbeat_timeout_s)
        device.status = ACTIVE if recent else (OFFLINE if device.last_seen else INACTIVE)
        device.save()
        if device.status == OFFLINE:
            self.alerts.raise_alert(device_id, "device_offline", {"lastSeen": to_iso(device.last_seen)})
        self._notify_maintenance(device, active=False)
        self.outbox.enqueue(
            ob.DEVICE_STATUS,
            {"deviceId": device_id, "event": "maintenance_ended", "by": actor, "durationSeconds": round(duration)},
        )
        return device

    def expire_maintenance(self, now=None) -> list[str]:
        """End maintenance windows whose time is up."""
        now = now or self.clock.now()
        due = list(Device.select().where(Device.status == MAINTENANCE, Device.maintenance_until <= now))
        for device in due:
            self.set_maintenance(device.device_id, False, actor="system:expired")
        return [d.device_id for d in due]

    def _notify_maintenance(self, device: Device, active: bool) -> None:
        params = {"active": active}
        if active:
            params["message"] = "Under maintenance"
        try:
            self.commands.send(device.device_id, "maintenance", params)
        except Exception:
            logger.exception("Could not notify %s about maintenance=%s", device.device_id, active)

    # --- clock --------------------------------------------------------------------
    def _sync_clock_if_drifted(self, device: Device, node_ts: float) -> None:
        now = self.clock.now()
        drift = abs(node_ts - to_epoch(now))
        if drift <= self.config.clock_drift_s:
            return
        last = self._last_time_sync.get(device.device_id)
        if last and now - last < timedelta(seconds=TIME_SYNC_COOLDOWN_S):
            return
        self._last_time_sync[device.device_id] = now
        logger.info("Node %s clock drifted %.1fs; sending time_sync", device.device_id, drift)
        self.commands.send(
            device.device_id,
            "time_sync",
            {"epoch": to_epoch(now), "timezone": self.config.timezone, "driftSeconds": round(drift, 1)},
        )

    @staticmethod
    def parse_node_time(seconds: float):
        return from_epoch(seconds)
