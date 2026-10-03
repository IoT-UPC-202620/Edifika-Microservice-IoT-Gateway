"""Local read access to sensor readings."""

from flask.views import MethodView
from flask_smorest import Blueprint

from ..models import SensorReading
from ..security import require_token
from .schemas import ReadingQuerySchema, ReadingSchema

blp = Blueprint("telemetry", __name__, url_prefix="/api/v1", description="Sensor readings")


@blp.route("/readings")
class Readings(MethodView):
    @require_token
    @blp.arguments(ReadingQuerySchema, location="query")
    @blp.response(200, ReadingSchema(many=True))
    def get(self, args):
        """Recent readings, newest first. Invalid readings are hidden unless requested."""
        query = SensorReading.select().order_by(SensorReading.captured_at.desc())
        if args["deviceId"]:
            query = query.where(SensorReading.device_id == args["deviceId"])
        if args["sensor"]:
            query = query.where(SensorReading.sensor == args["sensor"])
        if not args["includeInvalid"]:
            query = query.where(SensorReading.valid == True)  # noqa: E712
        return list(query.limit(args["limit"]))
