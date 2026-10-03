"""Credential administration and access log endpoints (usable without internet)."""

from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint

from ..models import AccessAttempt
from ..security import require_token
from .schemas import (
    AttemptQuerySchema,
    AttemptSchema,
    CredentialCreateSchema,
    CredentialSchema,
    CredentialStatusSchema,
)

blp = Blueprint("access", __name__, url_prefix="/api/v1", description="Credentials and access log")


def _gateway():
    return current_app.extensions["gateway"]


@blp.route("/credentials")
class CredentialCollection(MethodView):
    @require_token
    @blp.response(200, CredentialSchema(many=True))
    def get(self):
        """List the cached credentials."""
        return _gateway().credentials.list_all()

    @require_token
    @blp.arguments(CredentialCreateSchema)
    @blp.response(201, CredentialSchema)
    def post(self, body):
        """Register a card manually (for example while there is no internet)."""
        return _gateway().credentials.register(
            body["uid"],
            body["residentId"],
            body["residentName"],
            body["credentialType"],
            body["validFrom"],
            body["validUntil"],
        )


@blp.route("/credentials/<uid>/status")
class CredentialStatus(MethodView):
    @require_token
    @blp.arguments(CredentialStatusSchema)
    @blp.response(200, CredentialSchema)
    def patch(self, body, uid):
        """Activate, suspend or revoke a credential. Takes effect on the next read."""
        return _gateway().credentials.set_status(uid, body["status"])


@blp.route("/access-attempts")
class AccessAttempts(MethodView):
    @require_token
    @blp.arguments(AttemptQuerySchema, location="query")
    @blp.response(200, AttemptSchema(many=True))
    def get(self, args):
        """Recent access attempts, newest first. Credentials are masked."""
        query = AccessAttempt.select().order_by(AccessAttempt.id.desc())
        if args["deviceId"]:
            query = query.where(AccessAttempt.device_id == args["deviceId"])
        if args["result"]:
            query = query.where(AccessAttempt.result == args["result"])
        return list(query.limit(args["limit"]))
