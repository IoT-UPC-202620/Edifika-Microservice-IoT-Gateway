"""Health endpoint used by Docker and operators."""

from flask.views import MethodView
from flask_smorest import Blueprint

blp = Blueprint("health", __name__, description="Gateway health")


@blp.route("/health")
class Health(MethodView):
    @blp.response(200)
    def get(self):
        """Report the service status and its dependencies."""
        from flask import current_app

        checks = {}
        for name, probe in current_app.extensions.get("health_probes", {}).items():
            try:
                checks[name] = probe()
            except Exception as exc:  # a failing probe must not break the endpoint
                checks[name] = {"ok": False, "error": str(exc)}
        ok = all(check.get("ok", True) for check in checks.values())
        return {"status": "ok" if ok else "degraded", "gatewayId": current_app.config["EDGE_CONFIG"].gateway_id, "checks": checks}
