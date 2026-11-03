"""MQTT contract between the ESP32 nodes and the Edge Gateway (schema version 1).

Node -> Gateway (published by the firmware):
    {prefix}/nodes/{deviceId}/heartbeat   {v, deviceId, ts, fw?}
    {prefix}/nodes/{deviceId}/access      {v, deviceId, ts, credentialType, credential}
    {prefix}/nodes/{deviceId}/readings    {v, deviceId, ts, sensor, value, unit}
    {prefix}/nodes/{deviceId}/ack         {v, deviceId, commandId, status, ts, detail?}
    {prefix}/nodes/{deviceId}/enrollment  {v, deviceId, ts, credentialType, credential}
        card read while the node is in enrollment mode: it is NOT an access attempt
    {prefix}/nodes/{deviceId}/announce    {v, deviceId, ts, mac, fw, model, capabilities, ip, rssi, pairingCode}
        the node introduces itself; an unregistered node stays pending (and inert) until adopted

Gateway -> Node (published by the gateway):
    {prefix}/nodes/{deviceId}/commands    {v, commandId, type, params, ts}
    types include: access_result, mode ({mode, color?}), rgb ({color}), unlock, display, buzzer, alert, maintenance, time_sync,
    enroll ({active, seconds?}: enrollment mode, the next card is reported on `enrollment`),
    identify ({seconds?}: blink and beep; the only command sent to a node that is not adopted)

`ts` is the node clock in epoch seconds. A reading `value` may be null when the
sensor produced no measurement (for example, an ultrasonic sensor without echo).
"""

import json
from datetime import datetime

from marshmallow import EXCLUDE, Schema, ValidationError, fields, validate

SCHEMA_VERSION = 1
CHANNELS = ("heartbeat", "access", "readings", "ack", "enrollment", "announce")


class ContractError(Exception):
    """A message that violates the contract and must be discarded."""

    def __init__(self, code: str, message: str, device_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.device_id = device_id


def node_topic(prefix: str, device_id: str, channel: str) -> str:
    return f"{prefix}/nodes/{device_id}/{channel}"


def command_topic(prefix: str, device_id: str) -> str:
    return node_topic(prefix, device_id, "commands")


def subscription_topics(prefix: str) -> list[str]:
    return [f"{prefix}/nodes/+/{channel}" for channel in CHANNELS]


def parse_topic(prefix: str, topic: str) -> tuple[str, str]:
    parts = topic.split("/")
    base = prefix.split("/")
    if (
        len(parts) != len(base) + 3
        or parts[: len(base)] != base
        or parts[len(base)] != "nodes"
        or parts[-1] not in CHANNELS
        or not parts[-2]
    ):
        raise ContractError("unknown_topic", f"Unexpected topic: {topic}")
    return parts[-2], parts[-1]


def parse_ts(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        iso_str = value
        if iso_str.endswith("Z") or iso_str.endswith("z"):
            iso_str = iso_str[:-1] + "+00:00"
        return datetime.fromisoformat(iso_str).timestamp()
    raise ValueError(f"Cannot parse timestamp from {value!r}")


class _Message(Schema):
    class Meta:
        unknown = EXCLUDE

    v = fields.Raw(required=True)
    deviceId = fields.String(
        load_default=None, allow_none=True, validate=validate.Length(min=1, max=64)
    )
    ts = fields.Raw(required=True, allow_none=True)


class HeartbeatSchema(_Message):
    deviceId = fields.String(required=True, validate=validate.Length(min=1, max=64))
    fw = fields.String(load_default=None, allow_none=True)
    # Door state as the node currently shows it; the gateway re-sends `mode` when it is stale.
    mode = fields.String(load_default=None, allow_none=True, validate=validate.OneOf(["FREE_ACCESS", "RESERVED"]))
    color = fields.String(
        load_default=None, allow_none=True, validate=validate.Regexp(r"^#[0-9a-fA-F]{6}$", error="Expected #RRGGBB")
    )


class AccessSchema(_Message):
    credentialType = fields.String(
        load_default="RFID", validate=validate.OneOf(["RFID", "MOBILE"])
    )
    credential = fields.String(required=True, validate=validate.Length(min=1, max=128))


class ReadingSchema(_Message):
    sensor = fields.String(
        required=True, validate=validate.OneOf(["humidity", "ultrasonic", "temperature"])
    )
    value = fields.Float(required=True, allow_none=True)
    unit = fields.String(load_default="")


class AckSchema(_Message):
    commandId = fields.String(required=True)
    status = fields.String(required=True, validate=validate.OneOf(["OK", "FAILED", "ERROR"]))
    detail = fields.String(load_default=None, allow_none=True)


class EnrollmentSchema(_Message):
    """Card captured in enrollment mode. Same shape as an access read, other meaning."""

    credentialType = fields.String(load_default="RFID", validate=validate.OneOf(["RFID"]))
    credential = fields.String(required=True, validate=validate.Length(min=1, max=128))


CAPABILITY_NAMES = ["RFID", "LOCK", "BUZZER", "OLED", "RGB_LED", "TEMPERATURE", "HUMIDITY"]


class AnnounceSchema(_Message):
    """A node introducing itself: identity, firmware and what it can do."""

    deviceId = fields.String(required=True, validate=validate.Length(min=1, max=64))
    mac = fields.String(
        required=True, validate=validate.Regexp(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", error="Expected AA:BB:CC:DD:EE:FF")
    )
    fw = fields.String(required=True, validate=validate.Length(min=1, max=32))
    model = fields.String(load_default=None, allow_none=True, validate=validate.Length(max=64))
    capabilities = fields.List(fields.String(validate=validate.OneOf(CAPABILITY_NAMES)), required=True)
    ip = fields.String(load_default=None, allow_none=True, validate=validate.Length(max=45))
    rssi = fields.Integer(load_default=None, allow_none=True)
    pairingCode = fields.String(required=True, validate=validate.Regexp(r"^[0-9]{4}$", error="Expected 4 digits"))


SCHEMAS = {
    "heartbeat": HeartbeatSchema(),
    "access": AccessSchema(),
    "readings": ReadingSchema(),
    "ack": AckSchema(),
    "enrollment": EnrollmentSchema(),
    "announce": AnnounceSchema(),
}


def parse_message(prefix: str, topic: str, raw: bytes | str) -> tuple[str, str, dict]:
    """Validate an inbound message and return (device_id, channel, data)."""
    device_id, channel = parse_topic(prefix, topic)
    try:
        body = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid_json", "Payload is not valid JSON", device_id) from exc
    if not isinstance(body, dict):
        raise ContractError("invalid_payload", "Payload must be a JSON object", device_id)
    raw_v = body.get("v")
    if raw_v != SCHEMA_VERSION and raw_v != str(SCHEMA_VERSION):
        raise ContractError(
            "unsupported_version",
            f"Unsupported schema version: {raw_v!r}",
            device_id,
        )
    try:
        data = SCHEMAS[channel].load(body)
    except ValidationError as exc:
        raise ContractError("invalid_payload", json.dumps(exc.messages), device_id) from exc

    try:
        data["ts"] = parse_ts(data["ts"])
    except Exception as exc:
        raise ContractError("invalid_payload", f"Invalid timestamp: {exc}", device_id) from exc

    if not data.get("deviceId"):
        data["deviceId"] = device_id
    elif data["deviceId"] != device_id:
        raise ContractError(
            "device_mismatch",
            f"Payload deviceId {data['deviceId']!r} does not match topic {device_id!r}",
            device_id,
        )

    if channel == "ack":
        if data.get("status") == "ERROR":
            data["status"] = "FAILED"
        if data.get("detail") == "":
            data["detail"] = None

    if channel == "readings":
        if data.get("unit") == "C":
            data["unit"] = "°C"

    data["v"] = SCHEMA_VERSION
    return device_id, channel, data
