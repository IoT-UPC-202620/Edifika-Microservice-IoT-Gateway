"""REST API blueprints."""

from flask import jsonify

from ..services.errors import DomainError


def register_blueprints(api) -> None:
    from . import access, devices, health

    api.register_blueprint(health.blp)
    api.register_blueprint(devices.blp)
    api.register_blueprint(access.blp)


def register_error_handlers(app) -> None:
    @app.errorhandler(DomainError)
    def _domain_error(err: DomainError):
        body = {"code": err.status, "status": err.status_name, "message": err.message}
        return jsonify(body), err.status
