"""Marshmallow schemas for the REST API (camelCase on the wire)."""

from marshmallow import Schema, ValidationError, fields, validate

from ..fields import IsoDateTime
from ..services.access import mask


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


class CredentialCreateSchema(Schema):
    uid = fields.String(required=True, validate=validate.Length(min=1, max=128))
    residentId = fields.String(required=True, validate=validate.Length(min=1, max=64))
    residentName = fields.String(load_default="")
    credentialType = fields.String(load_default="RFID", validate=validate.OneOf(["RFID", "MOBILE"]))
    validFrom = IsoDateTime(load_default=None, allow_none=True)
    validUntil = IsoDateTime(load_default=None, allow_none=True)


class CredentialStatusSchema(Schema):
    status = fields.String(required=True, validate=validate.OneOf(["ACTIVE", "SUSPENDED", "REVOKED"]))


class CredentialSchema(Schema):
    uid = fields.String()
    credentialType = fields.String(attribute="credential_type")
    residentId = fields.String(attribute="resident_id")
    residentName = fields.String(attribute="resident_name")
    status = fields.String()
    validFrom = IsoDateTime(attribute="valid_from")
    validUntil = IsoDateTime(attribute="valid_until")
    updatedAt = IsoDateTime(attribute="updated_at")


class AttemptQuerySchema(Schema):
    deviceId = fields.String(load_default=None)
    result = fields.String(load_default=None, validate=validate.OneOf(["GRANTED", "DENIED"]))
    limit = fields.Integer(load_default=50, validate=validate.Range(min=1, max=500))


class AttemptSchema(Schema):
    id = fields.Integer()
    deviceId = fields.String(attribute="device_id")
    credential = fields.Method("masked_credential")
    residentId = fields.String(attribute="resident_id", allow_none=True)
    result = fields.String()
    reason = fields.String()
    occurredAt = IsoDateTime(attribute="occurred_at")

    def masked_credential(self, obj):
        return mask(obj.credential)


class DeviceSettingsSchema(Schema):
    humidity_low_pct = fields.Float(validate=validate.Range(min=0, max=100))
    tank_height_cm = fields.Float(validate=validate.Range(min=10, max=400))
    critical_level_pct = fields.Float(validate=validate.Range(min=0, max=100))
    hysteresis_pct = fields.Float(validate=validate.Range(min=0, max=50))


class ReadingQuerySchema(Schema):
    deviceId = fields.String(load_default=None)
    sensor = fields.String(load_default=None, validate=validate.OneOf(["humidity", "ultrasonic"]))
    includeInvalid = fields.Boolean(load_default=False)
    limit = fields.Integer(load_default=100, validate=validate.Range(min=1, max=1000))


class ReadingSchema(Schema):
    deviceId = fields.String(attribute="device_id")
    sensor = fields.String()
    value = fields.Float()
    unit = fields.String()
    levelPct = fields.Float(attribute="derived", allow_none=True)
    valid = fields.Boolean()
    capturedAt = IsoDateTime(attribute="captured_at")
