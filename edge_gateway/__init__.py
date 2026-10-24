"""Edifika Edge Gateway: transports data between the IoT nodes and the cloud backend."""

import atexit
import logging

from flask import Flask, g
from flask_smorest import Api

from .config import Config

__version__ = "0.3.0"

logger = logging.getLogger(__name__)


def create_app(
    config: Config | None = None,
    *,
    clock=None,
    cloud=None,
    events=None,
    sync_client=None,
) -> Flask:
    """Application factory.

    Fails fast with a clear message when a required setting is missing.
    """
    config = config or Config.from_env()
    config.validate()

    app = Flask(__name__)
    app.config["EDGE_CONFIG"] = config
    app.config.update(
        API_TITLE="Edifika Edge Gateway API",
        API_VERSION="v1",
        OPENAPI_VERSION="3.0.3",
        OPENAPI_URL_PREFIX="/",
        OPENAPI_JSON_PATH="openapi.json",
        OPENAPI_SWAGGER_UI_PATH="/docs",
        OPENAPI_SWAGGER_UI_URL="https://cdn.jsdelivr.net/npm/swagger-ui-dist/",
        JSON_SORT_KEYS=False,
    )
    app.json.sort_keys = False
    app.extensions["health_probes"] = {}

    from . import db
    from .gateway import Gateway

    db.init_database(config.database_path)
    app.extensions["health_probes"]["database"] = db.probe
    gateway = Gateway(
        config, clock=clock, cloud=cloud, events=events, sync_client=sync_client
    )
    app.extensions["gateway"] = gateway
    app.extensions["health_probes"]["outbox"] = gateway.outbox_probe
    app.extensions["health_probes"]["cache"] = gateway.cache_probe

    if config.mqtt_enabled:
        from .mqtt.bridge import MqttBridge

        bridge = MqttBridge(config, gateway.router)
        gateway.use_publisher(bridge)
        app.extensions["mqtt_bridge"] = bridge
        app.extensions["health_probes"]["mqtt"] = bridge.probe
        bridge.start()
        atexit.register(bridge.stop)

    @app.before_request
    def _open_connection():
        # Nested contexts are safe: the connection closes only when the outermost one exits.
        g.db_context = db.database.connection_context()
        g.db_context.__enter__()

    @app.teardown_request
    def _close_connection(_exc):
        context = g.pop("db_context", None)
        if context is not None:
            context.__exit__(None, None, None)

    api = Api(app)
    api.spec.components.security_scheme(
        "BearerAuth", {"type": "http", "scheme": "bearer"}
    )

    api.spec.options["security"] = [{"BearerAuth": []}]

    from .api import register_blueprints, register_error_handlers

    register_blueprints(api)
    register_error_handlers(app)

    if config.background_enabled:
        gateway.start_background()
        atexit.register(gateway.stop_background)
    return app
