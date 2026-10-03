import pytest

from edge_gateway import create_app
from edge_gateway.config import Config, ConfigError


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["gatewayId"] == "edge-gateway-01"


def test_swagger_ui_and_openapi_are_served(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").get_json()
    assert "/health" in spec["paths"]
    assert "BearerAuth" in spec["components"]["securitySchemes"]


def test_health_reports_degraded_when_a_probe_fails(app, client):
    app.extensions["health_probes"]["db"] = lambda: {"ok": False}
    assert client.get("/health").get_json()["status"] == "degraded"


def test_missing_service_token_fails_to_start():
    with pytest.raises(ConfigError, match="EDGE_SERVICE_TOKEN"):
        create_app(Config(service_token=""))


def test_invalid_numeric_env_value_is_reported():
    with pytest.raises(ConfigError, match="EDGE_MQTT_PORT"):
        Config.from_env({"EDGE_SERVICE_TOKEN": "x", "EDGE_MQTT_PORT": "abc"})


def test_config_reads_typed_values_from_env():
    config = Config.from_env(
        {"EDGE_SERVICE_TOKEN": "x", "EDGE_MQTT_PORT": "1884", "EDGE_MQTT_ENABLED": "false"}
    )
    assert config.mqtt_port == 1884
    assert config.mqtt_enabled is False
