"""MQTT contract between the ESP32 nodes and the Edge Gateway (schema version 1).

Node -> Gateway (published by the firmware):
    {prefix}/nodes/{deviceId}/heartbeat   {v, deviceId, ts, fw?}
    {prefix}/nodes/{deviceId}/access      {v, deviceId, ts, credentialType, credential}
    {prefix}/nodes/{deviceId}/readings    {v, deviceId, ts, sensor, value, unit}
    {prefix}/nodes/{deviceId}/ack         {v, deviceId, commandId, status, ts, detail?}

Gateway -> Node (published by the gateway):
    {prefix}/nodes/{deviceId}/commands    {v, commandId, type, params, ts}
    types include: access_result, mode ({mode, color?}), rgb ({color}), unlock, display, buzzer, alert, maintenance, time_sync

`ts` is the node clock in epoch seconds. A reading `value` may be null when the
sensor produced no measurement (for example, an ultrasonic sensor without echo).
"""

import json

from marshmallow import EXCLUDE, Schema, ValidationError, fields, validate

SCHEMA_VERSION = 1
CHANNELS = ("heartbeat", "access", "readings", "ack")


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


class _Message(Schema):
    class Meta:
        unknown = EXCLUDE

    v = fields.Integer(required=True)
    deviceId = fields.String(required=True, validate=validate.Length(min=1, max=64))
    ts = fields.Float(required=True)


class HeartbeatSchema(_Message):
    fw = fields.String(load_default=None, allow_none=True)


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
    status = fields.String(required=True, validate=validate.OneOf(["OK", "FAILED"]))
    detail = fields.String(load_default=None, allow_none=True)


SCHEMAS = {
    "heartbeat": HeartbeatSchema(),
    "access": AccessSchema(),
    "readings": ReadingSchema(),
    "ack": AckSchema(),
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
    if body.get("v") != SCHEMA_VERSION:
        raise ContractError(
            "unsupported_version",
            f"Unsupported schema version: {body.get('v')!r}",
            device_id,
        )
    try:
        data = SCHEMAS[channel].load(body)
    except ValidationError as exc:
        raise ContractError("invalid_payload", json.dumps(exc.messages), device_id) from exc
    if data["deviceId"] != device_id:
        raise ContractError(
            "device_mismatch",
            f"Payload deviceId {data['deviceId']!r} does not match topic {device_id!r}",
            device_id,
        )
    return device_id, channel, data
