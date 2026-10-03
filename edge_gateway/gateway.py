"""Composition root: builds the services and wires their dependencies."""

from .clock import SystemClock
from .mqtt.bridge import LoggingPublisher, PublisherRef
from .mqtt.router import MessageRouter
from .services.alerts import AlertService
from .services.commands import CommandService
from .services.outbox import OutboxService


class Gateway:
    def __init__(self, config, clock=None, publisher=None):
        self.config = config
        self.clock = clock or SystemClock()
        self.publisher = PublisherRef(publisher or LoggingPublisher())

        self.outbox = OutboxService(config, self.clock)
        self.alerts = AlertService(self.clock, self.outbox)
        self.commands = CommandService(config, self.clock, self.publisher, self.outbox)

        self.router = MessageRouter(config.mqtt_topic_prefix, on_rejected=self._on_rejected)
        self.router.register("ack", self.commands.handle_ack)

    def use_publisher(self, publisher) -> None:
        self.publisher.target = publisher

    def _on_rejected(self, err) -> None:
        # A node speaking an unknown schema version is reported once, not per message.
        if err.code == "unsupported_version" and err.device_id:
            self.alerts.raise_alert(err.device_id, "firmware_incompatible")
