from dataclasses import replace
import logging

import pytest

from edge_gateway.config import Config

BASE = {"EDGE_SERVICE_TOKEN": "t"}


def test_amqp_and_sync_settings_come_from_env():
    c = Config.from_env(
        {
            **BASE,
            "EDGE_AMQP_URL": "amqps://u:p@h:5671/v",
            "EDGE_SYNC_BASE_URL": "http://access:8089",
            "EDGE_SYNC_API_KEY": "k",
            "EDGE_MQTT_TLS": "true",
        }
    )
    assert (c.amqp_url, c.sync_base_url, c.sync_api_key, c.mqtt_tls) == (
        "amqps://u:p@h:5671/v",
        "http://access:8089",
        "k",
        True,
    )
    assert (c.amqp_events_exchange, c.amqp_commands_exchange) == (
        "edifika.events",
        "edifika.edge.commands",
    )


def test_legacy_cloud_variables_map_to_sync(caplog):
    with caplog.at_level(logging.WARNING):
        c = Config.from_env({**BASE, "EDGE_CLOUD_BASE_URL": "http://old", "EDGE_CLOUD_TOKEN": "tok"})
    assert (c.sync_base_url, c.sync_api_key) == ("http://old", "tok") and "deprecated" in caplog.text


def test_new_sync_variables_win_over_legacy():
    assert (
        Config.from_env(
            {**BASE, "EDGE_CLOUD_BASE_URL": "http://old", "EDGE_SYNC_BASE_URL": "http://new"}
        ).sync_base_url
        == "http://new"
    )
