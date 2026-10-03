"""Device registry endpoints."""

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..security import require_token
from .schemas import (
    CommandRequestSchema,
    CommandResultSchema,
    DeviceCreateSchema,
    DeviceSchema,
    DeviceSettingsSchema,
    MaintenanceSchema,
)

blp = Blueprint("devices", __name__, url_prefix="/api/v1/devices", description="Node registry")


def _devices():
    return current_app.extensions["gateway"].devices


@blp.route("")
class DeviceCollection(MethodView):
    @require_token
    @blp.response(200, DeviceSchema(many=True))
    def get(self):
        """List the registered nodes."""
        return _devices().list_all()

    @require_token
    @blp.arguments(DeviceCreateSchema)
    @blp.response(201, DeviceSchema)
    def post(self, body):
        """Register a node. It stays INACTIVE until its first heartbeat."""
        return _devices().register(
            body["deviceId"], body["areaId"], body["location"], body["capabilities"]
        )


@blp.route("/<device_id>")
class DeviceItem(MethodView):
    @require_token
    @blp.response(200, DeviceSchema)
    def get(self, device_id):
        """Get one node."""
        return _devices().require(device_id)


@blp.route("/<device_id>/settings")
class DeviceSettings(MethodView):
    @require_token
    @blp.arguments(DeviceSettingsSchema)
    @blp.response(200, DeviceSchema)
    def patch(self, body, device_id):
        """Calibrate a node: tank height, low humidity and critical level thresholds."""
        return _devices().update_settings(device_id, body)


@blp.route("/<device_id>/maintenance")
class DeviceMaintenance(MethodView):
    @require_token
    @blp.arguments(MaintenanceSchema)
    @blp.response(200, DeviceSchema)
    def put(self, body, device_id):
        """Put a node in maintenance (deactivated) or return it to operation."""
        return _devices().set_maintenance(
            device_id, body["enabled"], body["durationMinutes"], body["requestedBy"]
        )


@blp.route("/<device_id>/commands")
class DeviceCommands(MethodView):
    @require_token
    @blp.arguments(CommandRequestSchema)
    @blp.response(200, CommandResultSchema)
    def post(self, body, device_id):
        """Run a command on a node (open the lock, show a message, sound the buzzer).

        Answers 409 when the node is offline or in maintenance and 504 when it does not confirm.
        """
        gateway = current_app.extensions["gateway"]
        return gateway.remote.execute(device_id, body["type"], body["params"], body["requestedBy"])
