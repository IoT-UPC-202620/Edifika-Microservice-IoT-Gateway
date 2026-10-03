import pytest

from edge_gateway import create_app
from edge_gateway.config import Config
from edge_gateway.db import database

from .helpers import FakeClock, FakePublisher

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
def clock():
    return FakeClock()


@pytest.fixture
def publisher():
    return FakePublisher()


@pytest.fixture
def app(config, clock, publisher):
    app = create_app(config, clock=clock)
    app.extensions["gateway"].use_publisher(publisher)
    return app


@pytest.fixture
def gw(app):
    """The gateway composition root, with an open database connection for direct service calls."""
    with database.connection_context():
        yield app.extensions["gateway"]


@pytest.fixture
def client(app):
    return app.test_client()
