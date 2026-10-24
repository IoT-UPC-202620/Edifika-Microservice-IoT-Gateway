"""Handler for cloud orders: sync requests and remote device commands."""

from collections import OrderedDict
import logging
import re

from ..clock import from_iso
from . import outbox as ob

logger = logging.getLogger(__name__)

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class CloudCommandHandler:
    def __init__(self, config, clock, devices, access, commands, sync, outbox):
        self.config = config
        self.clock = clock
        self.devices = devices
        self.access = access
        self.commands = commands
        self.sync = sync
        self.outbox = outbox
        self._seen_commands: OrderedDict[str, bool] = OrderedDict()

    def _mark_seen(self, command_id: str) -> bool:
        """Returns True if already seen; otherwise marks it seen and returns False."""
        if not command_id:
            return False
        if command_id in self._seen_commands:
            return True
        self._seen_commands[command_id] = True
        if len(self._seen_commands) > 1000:
            self._seen_commands.popitem(last=False)
        return False

    def _report(self, device_id: str, command_id: str, type_: str, status: str, detail: str | None) -> None:
        self.outbox.enqueue(
            ob.COMMAND_RESULT,
            {
                "deviceId": device_id,
                "commandId": command_id,
                "type": type_,
                "status": status,
                "detail": detail,
            },
        )

    def handle(self, envelope: dict) -> None:
        event_type = envelope.get("eventType")
        payload = envelope.get("payload", {})

        if event_type == "SyncRequested":
            self.sync.pull()
            return

        if event_type == "DeviceCommandRequested":
            command_id = payload.get("commandId", "")
            device_code = payload.get("deviceCode", "")
            type_ = payload.get("type", "")
            params = payload.get("params") or {}
            expires_at_str = payload.get("expiresAt")

            # 1. Deduplication (LRU 1000)
            if self._mark_seen(command_id):
                logger.debug("Ignoring duplicate cloud command %s", command_id)
                return

            now = self.clock.now()

            # 2. Expiration
            if expires_at_str:
                expires_at = from_iso(expires_at_str)
                if expires_at and expires_at <= now:
                    self._report(device_code, command_id, type_, "EXPIRED", None)
                    return

            # 3. Unknown device
            device = self.devices.get(device_code)
            if device is None:
                self._report(device_code, command_id, type_, "FAILED", "unknown device")
                return

            # 4. Unsupported command type
            if type_ != "rgb":
                self._report(device_code, command_id, type_, "FAILED", "unsupported command")
                return

            # 5. Invalid color
            color = params.get("color") if isinstance(params, dict) else None
            if not color or not isinstance(color, str) or not COLOR_RE.match(color):
                self._report(device_code, command_id, type_, "FAILED", "invalid color")
                return
            color = color.upper()

            # 6. Door mode is not RESERVED
            current_mode = self.access.current_mode(device, now)
            if current_mode != "RESERVED":
                self._report(device_code, command_id, type_, "FAILED", "door is not reserved")
                return

            # 7. Device status offline, maintenance or inactive
            if device.status in ("OFFLINE", "MAINTENANCE", "INACTIVE"):
                self._report(device_code, command_id, type_, "FAILED", "device unavailable")
                return

            # 8. All checks pass
            device.led_color = color
            device.save()
            self.commands.send(
                device_code, "rgb", {"color": color}, command_id=command_id, report=True
            )
