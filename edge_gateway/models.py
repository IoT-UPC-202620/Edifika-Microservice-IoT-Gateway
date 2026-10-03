"""Peewee models: local cache of the cloud state plus the outbound event queue."""

import json

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    IntegerField,
    Model,
    TextField,
)

from .db import database


class BaseModel(Model):
    class Meta:
        database = database


class JsonMixin:
    @staticmethod
    def _load(raw, default):
        try:
            return json.loads(raw) if raw else default
        except json.JSONDecodeError:
            return default


class Device(BaseModel, JsonMixin):
    """An ESP32 node registered in this gateway."""

    device_id = CharField(primary_key=True)
    area_id = CharField(null=True)  # common area the node belongs to
    location = CharField(default="")
    capabilities = TextField(default="[]")  # JSON list: rfid, lock, buzzer, oled, humidity, ultrasonic
    status = CharField(default="INACTIVE")  # INACTIVE | ACTIVE | OFFLINE | MAINTENANCE
    last_seen = DateTimeField(null=True)
    firmware = CharField(null=True)
    maintenance_since = DateTimeField(null=True)
    maintenance_until = DateTimeField(null=True)
    config = TextField(default="{}")  # JSON: calibration and thresholds

    @property
    def capability_list(self) -> list:
        return self._load(self.capabilities, [])

    @property
    def settings(self) -> dict:
        return self._load(self.config, {})


class Credential(BaseModel):
    """Access credential cached from the cloud (RFID card or mobile token id)."""

    uid = CharField(primary_key=True)
    credential_type = CharField(default="RFID")  # RFID | MOBILE
    resident_id = CharField()
    resident_name = CharField(default="")
    status = CharField(default="ACTIVE")  # ACTIVE | SUSPENDED | REVOKED
    valid_from = DateTimeField(null=True)
    valid_until = DateTimeField(null=True)
    updated_at = DateTimeField()


class AccessPermission(BaseModel):
    """Time window in which a resident may enter an area (derived from a reservation)."""

    source_id = CharField(unique=True)  # reservation id in the cloud
    resident_id = CharField(index=True)
    area_id = CharField(index=True)
    starts_at = DateTimeField()
    ends_at = DateTimeField()


class AreaSchedule(BaseModel):
    """Daily opening hours of an area, in the gateway timezone ("HH:MM")."""

    area_id = CharField(primary_key=True)
    opens_at = CharField()
    closes_at = CharField()


class AccessAttempt(BaseModel):
    id = AutoField()
    device_id = CharField(index=True)
    credential = CharField()
    resident_id = CharField(null=True)
    result = CharField()  # GRANTED | DENIED
    reason = CharField()
    occurred_at = DateTimeField(index=True)


class SensorReading(BaseModel):
    id = AutoField()
    device_id = CharField()
    sensor = CharField()  # humidity | ultrasonic
    value = FloatField()
    unit = CharField(default="")
    captured_at = DateTimeField()
    valid = BooleanField(default=True)
    derived = FloatField(null=True)  # water level percentage for ultrasonic readings

    class Meta:
        indexes = ((("device_id", "sensor", "captured_at"), False),)


class ActiveAlert(BaseModel):
    """Alerts currently raised, so each one is announced once and cleared once."""

    device_id = CharField()
    kind = CharField()
    raised_at = DateTimeField()

    class Meta:
        indexes = ((("device_id", "kind"), True),)


class Outbox(BaseModel):
    """Events waiting to be delivered to the cloud, in chronological order."""

    id = AutoField()
    event_id = CharField(unique=True)
    kind = CharField(index=True)
    payload = TextField()
    occurred_at = DateTimeField()
    created_at = DateTimeField()
    sent_at = DateTimeField(null=True, index=True)
    attempts = IntegerField(default=0)
    last_error = TextField(null=True)


class SyncState(BaseModel):
    key = CharField(primary_key=True)
    value = TextField()


ALL_MODELS = [
    Device,
    Credential,
    AccessPermission,
    AreaSchedule,
    AccessAttempt,
    SensorReading,
    ActiveAlert,
    Outbox,
    SyncState,
]
