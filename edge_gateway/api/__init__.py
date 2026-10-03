"""REST API blueprints."""


def register_blueprints(api) -> None:
    from . import health

    api.register_blueprint(health.blp)
