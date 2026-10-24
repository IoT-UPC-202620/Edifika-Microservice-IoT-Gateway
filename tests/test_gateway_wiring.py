from dataclasses import replace

import pytest

from edge_gateway.config import Config
from edge_gateway.gateway import Gateway
from edge_gateway.services.amqp import AmqpEventPublisher
from edge_gateway.services.cloud import SyncClient
from tests.helpers import FakeClock


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response

    @property
    def last(self):
        m, u, kw = self.requests[-1]
        return (m, u, kw.get("headers"), kw.get("params"))


def test_amqp_url_builds_amqp_event_sink(config):
    assert isinstance(
        Gateway(replace(config, amqp_url="amqp://x"), clock=FakeClock()).forwarder.cloud,
        AmqpEventPublisher,
    )


def test_sync_base_url_builds_sync_client(config):
    assert isinstance(
        Gateway(
            replace(config, sync_base_url="http://a", sync_api_key="k"), clock=FakeClock()
        ).sync.cloud,
        SyncClient,
    )


def test_sync_client_sends_gateway_headers():
    s = FakeSession(FakeResponse(200, {"version": 1}))
    SyncClient(
        Config(service_token="t", gateway_id="gw-1", sync_base_url="http://a", sync_api_key="k"), s
    ).fetch_sync(3)
    assert s.last == (
        "GET",
        "http://a/api/v1/edge/sync",
        {"X-Gateway-Id": "gw-1", "Authorization": "Bearer k"},
        {"since": 3},
    )


def test_without_amqp_url_events_stay_queued(config):
    assert Gateway(config, clock=FakeClock()).forwarder.cloud is None
