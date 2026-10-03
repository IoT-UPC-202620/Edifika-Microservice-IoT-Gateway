"""Commands requested by the cloud or an administrator and executed on a node."""

import logging

from ..mqtt.bridge import PublishError
from . import outbox as ob
from .errors import BrokerUnavailable, CommandNotConfirmed, DeviceUnavailable, ValidationFailed

logger = logging.getLogger(__name__)

BUZZER_PATTERNS = ("granted", "denied", "alert", "off")

# command type -> capability the node must declare
REQUIRED_CAPABILITY = {"unlock": "lock", "display": "oled", "buzzer": "buzzer"}


class RemoteCommandService:
    def __init__(self, config, devices, commands, outbox):
        self.config = config
        self.devices = devices
        self.commands = commands
        self.outbox = outbox

    def execute(self, device_id: str, type_: str, params: dict | None = None, requested_by: str = "api") -> dict:
        """Run a command on a node and wait for its acknowledgement."""
        params = self._validate(type_, params or {})
        device = self.devices.require(device_id)
        if REQUIRED_CAPABILITY[type_] not in device.capability_list:
            raise ValidationFailed(f"The device has no {REQUIRED_CAPABILITY[type_]} capability")
        # Nothing is queued for later: a delayed opening would surprise whoever is at the door.
        if device.status == "MAINTENANCE":
            raise DeviceUnavailable("Device under maintenance")
        if device.status != "ACTIVE":
            raise DeviceUnavailable("Device not available")

        try:
            pending = self.commands.send_and_wait(
                device_id, type_, params, timeout=self.config.remote_command_timeout_s
            )
        except PublishError as exc:
            raise BrokerUnavailable("The MQTT broker is not available") from exc

        base = {"deviceId": device_id, "type": type_, "requestedBy": requested_by, "source": "remote"}
        if pending is None:
            self.outbox.enqueue(ob.COMMAND_RESULT, {**base, "status": "TIMEOUT"})
            raise CommandNotConfirmed("The device did not confirm the command")
        if pending.status == "FAILED":  # the ack handler already queued the failure
            raise CommandNotConfirmed("The device reported a failure executing the command")
        self.outbox.enqueue(
            ob.COMMAND_RESULT, {**base, "commandId": pending.command_id, "status": "ACKED"}
        )
        return {"commandId": pending.command_id, "status": "ACKED"}

    def _validate(self, type_: str, params: dict) -> dict:
        if type_ == "unlock":
            duration = params.get("durationMs", self.config.lock_open_ms)
            if not isinstance(duration, int) or not 500 <= duration <= 30000:
                raise ValidationFailed("durationMs must be an integer between 500 and 30000")
            return {"durationMs": duration}
        if type_ == "display":
            message = params.get("message")
            if not isinstance(message, str) or not 1 <= len(message) <= 64:
                raise ValidationFailed("message must be a text of 1 to 64 characters")
            return {"message": message}
        if type_ == "buzzer":
            pattern = params.get("pattern")
            if pattern not in BUZZER_PATTERNS:
                raise ValidationFailed(f"pattern must be one of {', '.join(BUZZER_PATTERNS)}")
            return {"pattern": pattern}
        raise ValidationFailed(f"Unsupported command type: {type_}")
