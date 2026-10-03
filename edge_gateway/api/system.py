"""Cloud synchronisation and local status endpoints."""

import json

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint
from marshmallow import Schema, fields, validate

from ..fields import IsoDateTime
from ..models import Device, Outbox
from ..security import require_token
from ..services.sync import SyncSchema

blp = Blueprint("system", __name__, url_prefix="/api/v1", description="Synchronisation and status")


def _gateway():
    return current_app.extensions["gateway"]


class OutboxQuerySchema(Schema):
    limit = fields.Integer(load_default=50, validate=validate.Range(min=1, max=500))


class OutboxEventSchema(Schema):
    eventId = fields.String(attribute="event_id")
    kind = fields.String()
    occurredAt = IsoDateTime(attribute="occurred_at")
    attempts = fields.Integer()
    lastError = fields.String(attribute="last_error", allow_none=True)
    payload = fields.Method("parsed_payload")

    def parsed_payload(self, obj):
        return json.loads(obj.payload)


@blp.route("/sync")
class Sync(MethodView):
    @require_token
    @blp.arguments(SyncSchema)
    @blp.response(200)
    def post(self, body):
        """Receive a credential snapshot or update pushed by the cloud backend."""
        return _gateway().sync.apply(body)


@blp.route("/status")
class Status(MethodView):
    @require_token
    @blp.response(200)
    def get(self):
        """Gateway state for local operation: queue, cache and devices (works without internet)."""
        gateway = _gateway()
        counts = {}
        for device in Device.select(Device.status):
            counts[device.status] = counts.get(device.status, 0) + 1
        bridge = current_app.extensions.get("mqtt_bridge")
        return {
            "gatewayId": gateway.config.gateway_id,
            "mqttConnected": bridge.connected if bridge else None,
            "outbox": gateway.forwarder.stats(),
            "cache": gateway.sync.status(),
            "devices": counts,
            "pendingCommands": len(gateway.commands.pending),
            "rejectedMessages": dict(gateway.router.rejected),
        }


@blp.route("/outbox")
class OutboxEvents(MethodView):
    @require_token
    @blp.arguments(OutboxQuerySchema, location="query")
    @blp.response(200, OutboxEventSchema(many=True))
    def get(self, args):
        """Events still waiting to be delivered to the cloud, oldest first."""
        return list(
            Outbox.select().where(Outbox.sent_at.is_null()).order_by(Outbox.id).limit(args["limit"])
        )
