"""Edifika Edge Gateway: transports data between the IoT nodes and the cloud backend."""

import atexit
import logging

from flask import Flask
from flask_smorest import Api

from .config import Config

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


def create_app(config: Config | None = None, *, clock=None) -> Flask:
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
    gateway = Gateway(config, clock=clock)
    app.extensions["gateway"] = gateway

    if config.mqtt_enabled:
        from .mqtt.bridge import MqttBridge

        bridge = MqttBridge(config, gateway.router)
        gateway.use_publisher(bridge)
        app.extensions["mqtt_bridge"] = bridge
        app.extensions["health_probes"]["mqtt"] = bridge.probe
        bridge.start()
        atexit.register(bridge.stop)

    @app.teardown_appcontext
    def _close_connection(_exc):
        if not db.database.is_closed():
            db.database.close()

    api = Api(app)
    api.spec.components.security_scheme(
        "BearerAuth", {"type": "http", "scheme": "bearer"}
    )

    from .api import register_blueprints

    register_blueprints(api)
    return app
