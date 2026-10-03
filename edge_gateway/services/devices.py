"""Node registry, liveness tracking and clock synchronisation."""

import json
import logging
from datetime import timedelta

from ..clock import from_epoch, to_epoch, to_iso
from ..models import Device
from . import outbox as ob
from .errors import ConflictError, NotFoundError, ValidationFailed

logger = logging.getLogger(__name__)

CAPABILITIES = {"rfid", "lock", "buzzer", "oled", "humidity", "ultrasonic", "rtc"}
TIME_SYNC_COOLDOWN_S = 30

ACTIVE = "ACTIVE"
INACTIVE = "INACTIVE"
OFFLINE = "OFFLINE"
MAINTENANCE = "MAINTENANCE"


class DeviceService:
    def __init__(self, config, clock, commands, outbox, alerts):
        self.config = config
        self.clock = clock
        self.commands = commands
        self.outbox = outbox
        self.alerts = alerts
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
        """Look up the sender of an MQTT message; unknown senders are reported once and ignored."""
        device = self.get(device_id)
        if device is None:
            logger.warning("Ignoring message from unregistered device %s", device_id)
            self.alerts.raise_alert(device_id, "unregistered_device")
        return device

    # --- liveness -----------------------------------------------------------------
    def touch(self, device: Device) -> Device:
        """Record activity from a node. A node in maintenance stays deactivated."""
        now = self.clock.now()
        device.last_seen = now
        if device.status != MAINTENANCE:
            device.status = ACTIVE
        device.save()
        if device.status == ACTIVE:
            self.alerts.clear_alert(device.device_id, "device_offline")
        self.alerts.clear_alert(device.device_id, "firmware_incompatible")
        return device

    def on_heartbeat(self, device_id: str, data: dict) -> None:
        device = self.known_device(device_id)
        if device is None:
            return
        if data.get("fw"):
            device.firmware = data["fw"]
        self.touch(device)
        if device.status != MAINTENANCE:
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
        return [d.device_id for d in silent]

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
