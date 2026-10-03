import pytest

from edge_gateway import create_app
from edge_gateway.config import Config

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def config(tmp_path):
    return Config(
        service_token=TOKEN,
        database_path=str(tmp_path / "edge.db"),
        mqtt_enabled=False,
        background_enabled=False,
    )


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    return app.test_client()
