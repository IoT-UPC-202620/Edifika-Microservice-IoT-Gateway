"""Custom marshmallow fields shared by the API and the sync contract."""

from marshmallow import ValidationError, fields

from .clock import from_iso, to_iso


class IsoDateTime(fields.Field):
    """Naive UTC datetime serialised as ISO 8601 with a Z suffix."""

    def _serialize(self, value, attr, obj, **kwargs):
        return to_iso(value)

    def _deserialize(self, value, attr, data, **kwargs):
        try:
            return from_iso(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("Not a valid ISO 8601 date-time") from exc
