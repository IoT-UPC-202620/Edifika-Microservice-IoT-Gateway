"""Composition root: builds the services and wires their dependencies."""

from .clock import SystemClock
from .mqtt.bridge import LoggingPublisher, PublisherRef
from .mqtt.router import MessageRouter
from .scheduler import Scheduler
from .services.access import AccessService
from .services.alerts import AlertService
from .services.amqp import AmqpEventPublisher
from .services.cloud import SyncClient
from .services.commands import CommandService
from .services.credentials import CredentialService
from .services.devices import DeviceService
from .services.outbox import OutboxForwarder, OutboxService
from .services.remote import RemoteCommandService
from .services.sync import SyncService
from .services.telemetry import TelemetryService


class Gateway:
    def __init__(
        self,
        config,
        clock=None,
        publisher=None,
        cloud=None,
        events=None,
        sync_client=None,
    ):
        self.config = config
        self.clock = clock or SystemClock()
        self.publisher = PublisherRef(publisher or LoggingPublisher())

        if cloud is not None:
            if events is None:
                events = cloud
            if sync_client is None:
                sync_client = cloud

        if events is None and config.amqp_url:
            events = AmqpEventPublisher(config)
        if sync_client is None and config.sync_base_url:
            sync_client = SyncClient(config)

        self.events = events
        self.sync_client = sync_client
        self.cloud = cloud

        self.outbox = OutboxService(config, self.clock)
        self.alerts = AlertService(self.clock, self.outbox)
        self.commands = CommandService(config, self.clock, self.publisher, self.outbox)
        self.devices = DeviceService(config, self.clock, self.commands, self.outbox, self.alerts)
        self.remote = RemoteCommandService(config, self.devices, self.commands, self.outbox)
        self.credentials = CredentialService(self.clock, self.outbox)
        self.access = AccessService(config, self.clock, self.devices, self.commands, self.outbox)
        self.telemetry = TelemetryService(
            config, self.clock, self.devices, self.commands, self.outbox, self.alerts
        )
        self.forwarder = OutboxForwarder(config, self.clock, self.outbox, events)
        self.sync = SyncService(config, self.clock, self.outbox, sync_client, self.alerts)

        self.router = MessageRouter(config.mqtt_topic_prefix, on_rejected=self._on_rejected)
        self.router.register("heartbeat", self.devices.on_heartbeat)
        self.router.register("access", self.access.on_access)
        self.router.register("readings", self.telemetry.on_reading)
        self.router.register("ack", self.commands.handle_ack)

        self.command_consumer = None
        if config.amqp_url:
            from .services.amqp import AmqpCommandConsumer
            from .services.cloud_commands import CloudCommandHandler

            handler = CloudCommandHandler(
                config,
                self.clock,
                self.devices,
                self.access,
                self.commands,
                self.sync,
                self.outbox,
            )
            self.command_consumer = AmqpCommandConsumer(config, handler)

        self.schedulers: list[Scheduler] = []

    def use_publisher(self, publisher) -> None:
        self.publisher.target = publisher

    def _on_rejected(self, err) -> None:
        # A node speaking an unknown schema version is reported once, not per message.
        if err.code == "unsupported_version" and err.device_id:
            self.alerts.raise_alert(err.device_id, "firmware_incompatible")

    # --- health -------------------------------------------------------------------
    def outbox_probe(self) -> dict:
        return {"ok": True, **self.forwarder.stats()}

    def cache_probe(self) -> dict:
        return {"ok": True, **self.sync.status()}

    # --- background work ----------------------------------------------------------
    def housekeeping(self) -> None:
        """Time-driven local checks: silent nodes, unanswered commands and a stale cache."""
        self.devices.check_offline()
        self.devices.expire_maintenance()
        self.commands.check_timeouts()
        self.sync.check_staleness()
        self.access.check_mode_changes()

    def start_background(self) -> None:
        # Cloud calls can block for their timeout, so they run apart from the local checks.
        local = Scheduler([("housekeeping", 1.0, self.housekeeping)])
        cloud = Scheduler(
            [
                ("outbox-flush", self.config.outbox_interval_s, self.forwarder.flush),
                ("cloud-sync", self.config.sync_interval_s, self.sync.pull),
            ]
        )
        self.schedulers = [local, cloud]
        for scheduler in self.schedulers:
            scheduler.start()
        if self.command_consumer is not None:
            self.command_consumer.start()

    def stop_background(self) -> None:
        if self.command_consumer is not None:
            self.command_consumer.stop()
        for scheduler in self.schedulers:
            scheduler.stop()
