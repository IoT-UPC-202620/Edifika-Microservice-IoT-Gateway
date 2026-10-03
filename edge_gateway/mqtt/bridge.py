"""MQTT client bridge (paho-mqtt) between the local broker and the router."""

import json
import logging

import paho.mqtt.client as mqtt

from . import contract

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """The command could not be handed to the broker."""


class LoggingPublisher:
    """Stand-in publisher used when MQTT is disabled. Keeps what it was asked to send."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def publish_command(self, device_id: str, command: dict) -> None:
        self.sent.append((device_id, command))
        logger.info("MQTT disabled; command for %s not published: %s", device_id, command)


class PublisherRef:
    """Late-bound reference so services can be built before the bridge exists."""

    def __init__(self, target):
        self.target = target

    def publish_command(self, device_id: str, command: dict) -> None:
        self.target.publish_command(device_id, command)


class MqttBridge:
    def __init__(self, config, router, client_factory=None):
        self.config = config
        self.router = router
        factory = client_factory or (
            lambda: mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"{config.gateway_id}",
            )
        )
        self.client = factory()
        if config.mqtt_username:
            self.client.username_pw_set(config.mqtt_username, config.mqtt_password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        # connect_async + loop_start keeps retrying while the broker is still starting.
        self.client.connect_async(self.config.mqtt_host, self.config.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            logger.debug("MQTT disconnect failed", exc_info=True)

    def publish_command(self, device_id: str, command: dict) -> None:
        if not self._connected:
            raise PublishError("MQTT broker is not connected")
        topic = contract.command_topic(self.config.mqtt_topic_prefix, device_id)
        info = self.client.publish(topic, json.dumps(command, separators=(",", ":")), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise PublishError(f"MQTT publish failed with code {info.rc}")

    def probe(self) -> dict:
        return {"ok": self._connected, "broker": f"{self.config.mqtt_host}:{self.config.mqtt_port}"}

    # paho callbacks (API v2 signatures)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT connection refused: %s", reason_code)
            return
        self._connected = True
        logger.info("Connected to MQTT broker %s:%s", self.config.mqtt_host, self.config.mqtt_port)
        for topic in contract.subscription_topics(self.config.mqtt_topic_prefix):
            client.subscribe(topic, qos=1)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected = False
        logger.warning("Disconnected from MQTT broker (%s)", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            self.router.handle(msg.topic, msg.payload)
        except Exception:  # never let a bad message kill the network loop
            logger.exception("Unhandled error while processing %s", msg.topic)
