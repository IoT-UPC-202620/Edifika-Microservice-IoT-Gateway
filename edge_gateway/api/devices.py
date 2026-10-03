"""Device registry endpoints."""

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..security import require_token
from .schemas import DeviceCreateSchema, DeviceSchema

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
