import json
import os
import queue
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import pika
import pytest

from edge_gateway import create_app
from edge_gateway.config import Config
from edge_gateway.db import database
from edge_gateway.gateway import Gateway
from edge_gateway.models import Credential, Device

EDGE_IT_AMQP_URL = os.environ.get("EDGE_IT_AMQP_URL")
EDGE_IT_MQTT_HOST = os.environ.get("EDGE_IT_MQTT_HOST")


@pytest.fixture(autouse=True)
def check_environment():
    if not EDGE_IT_AMQP_URL or not EDGE_IT_MQTT_HOST:
        pytest.skip("EDGE_IT_AMQP_URL and EDGE_IT_MQTT_HOST are required for integration tests")


@pytest.fixture
def amqp_url():
    return EDGE_IT_AMQP_URL


@pytest.fixture
def mqtt_host():
    return EDGE_IT_MQTT_HOST


class AmqpSpy:
    def __init__(self, amqp_url: str, exchange: str, routing_keys: list[str]):
        self.amqp_url = amqp_url
        self.exchange = exchange
        self.routing_keys = routing_keys
        self.queue_name = None
        self._conn = None
        self._ch = None

    def start(self):
        self._conn = pika.BlockingConnection(pika.URLParameters(self.amqp_url))
        self._ch = self._conn.channel()
        res = self._ch.queue_declare(queue="", exclusive=True)
        self.queue_name = res.method.queue
        for rk in self.routing_keys:
            self._ch.queue_bind(queue=self.queue_name, exchange=self.exchange, routing_key=rk)

    def get(self, timeout: float = 6.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            method, props, body = self._ch.basic_get(queue=self.queue_name, auto_ack=True)
            if method is not None and body:
                return json.loads(body.decode("utf-8"))
            time.sleep(0.05)
        raise TimeoutError(f"No message received on queue {self.queue_name} within {timeout}s")

    def stop(self):
        if self._ch is not None and not getattr(self._ch, "is_closed", False):
            try:
                self._ch.close()
            except Exception:
                pass
        if self._conn is not None and not getattr(self._conn, "is_closed", False):
            try:
                self._conn.close()
            except Exception:
                pass


@pytest.fixture
def spy(amqp_url):
    s = AmqpSpy(amqp_url, "edifika.events", ["edge.access-attempt", "edge.#"])
    s.start()
    yield s
    s.stop()


@pytest.fixture
def spy_results(amqp_url):
    s = AmqpSpy(amqp_url, "edifika.events", ["edge.command-result"])
    s.start()
    yield s
    s.stop()


class MqttSpy:
    def __init__(self, host: str, port: int = 1883, prefix: str = "edifika/v1"):
        self.host = host
        self.port = port
        self.prefix = prefix
        self.messages = queue.Queue()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"mqtt-spy-{os.getpid()}-{int(time.time()*1000)%10000}",
        )
        self.client.on_message = self._on_message

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            self.messages.put((msg.topic, payload))
        except Exception:
            pass

    def start(self):
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.subscribe(f"{self.prefix}/nodes/+/commands", qos=1)
        self.client.loop_start()

    def get(self, timeout: float = 5.0) -> tuple[str, dict]:
        return self.messages.get(timeout=timeout)

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def mqtt_spy(mqtt_host):
    s = MqttSpy(mqtt_host)
    s.start()
    time.sleep(0.3)
    yield s
    s.stop()


class IntegrationGatewayHelper:
    def __init__(self, gw: Gateway, mqtt_host: str, prefix: str = "edifika/v1"):
        self.gw = gw
        self.mqtt_host = mqtt_host
        self.prefix = prefix
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"it-node-{os.getpid()}-{int(time.time()*1000)%10000}",
        )
        self.client.connect(mqtt_host, 1883, keepalive=30)
        self.client.loop_start()

    def tap(self, device_id: str, uid: str):
        topic = f"{self.prefix}/nodes/{device_id}/access"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {"v": "1", "ts": now_iso, "credentialType": "RFID", "credential": uid}
        self.client.publish(topic, json.dumps(payload), qos=1)

    def publish_node_msg(self, device_id: str, channel: str, **fields):
        topic = f"{self.prefix}/nodes/{device_id}/{channel}"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"v": "1", "ts": now_iso, **fields}
        self.client.publish(topic, json.dumps(body), qos=1)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def it_gateway(tmp_path, amqp_url, mqtt_host):
    db_path = str(tmp_path / "it_edge.db")
    config = Config(
        gateway_id="edge-gateway-01",
        service_token="it-token",
        database_path=db_path,
        mqtt_enabled=True,
        mqtt_host=mqtt_host,
        mqtt_port=1883,
        amqp_url=amqp_url,
        background_enabled=True,
        outbox_interval_s=0.5,
    )
    app = create_app(config)
    gw = app.extensions["gateway"]

    # Pre-populate device and credentials in database
    with database.connection_context():
        Device.create(
            device_id="esp32-acc-01",
            area_id="pool",
            capabilities=json.dumps(["rfid", "lock", "oled", "buzzer", "rgb_led", "temperature", "humidity"]),
            status="ACTIVE",
            managed="cloud",
            current_mode="FREE_ACCESS",
        )
        Credential.create(
            uid="04A1B2C3",
            credential_type="RFID",
            resident_id="15",
            resident_name="Ana Perez",
            status="ACTIVE",
            updated_at=datetime.now(timezone.utc),
        )

    helper = IntegrationGatewayHelper(gw, mqtt_host)
    time.sleep(1.0)
    yield helper
    helper.close()
    if "mqtt_bridge" in app.extensions:
        app.extensions["mqtt_bridge"].stop()
    gw.stop_background()
