"""Marshmallow schemas for the REST API (camelCase on the wire)."""

from marshmallow import Schema, fields, validate

from ..clock import to_iso


class IsoDateTime(fields.Field):
    """Naive UTC datetime serialised as ISO 8601 with a Z suffix."""

    def _serialize(self, value, attr, obj, **kwargs):
        return to_iso(value)


class DeviceCreateSchema(Schema):
    deviceId = fields.String(required=True, validate=validate.Length(min=1, max=64))
    areaId = fields.String(load_default=None, allow_none=True)
    location = fields.String(load_default="")
    capabilities = fields.List(fields.String(), load_default=list)


class DeviceSchema(Schema):
    deviceId = fields.String(attribute="device_id")
    areaId = fields.String(attribute="area_id", allow_none=True)
    location = fields.String()
    capabilities = fields.List(fields.String(), attribute="capability_list")
    status = fields.String()
    lastSeen = IsoDateTime(attribute="last_seen")
    firmware = fields.String(allow_none=True)
    maintenanceUntil = IsoDateTime(attribute="maintenance_until")
    settings = fields.Dict()
