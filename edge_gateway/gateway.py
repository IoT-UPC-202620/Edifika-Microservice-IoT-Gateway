"""Composition root: builds the services and wires their dependencies."""

from .clock import SystemClock
from .mqtt.bridge import LoggingPublisher, PublisherRef
from .mqtt.router import MessageRouter
from .scheduler import Scheduler
from .services.access import AccessService
from .services.alerts import AlertService
from .services.cloud import CloudClient
from .services.commands import CommandService
from .services.credentials import CredentialService
from .services.devices import DeviceService
from .services.outbox import OutboxForwarder, OutboxService
from .services.sync import SyncService
from .services.telemetry import TelemetryService


class Gateway:
    def __init__(self, config, clock=None, publisher=None, cloud=None):
        self.config = config
        self.clock = clock or SystemClock()
        self.publisher = PublisherRef(publisher or LoggingPublisher())
        if cloud is None and config.cloud_base_url:
            cloud = CloudClient(config)
        self.cloud = cloud

        self.outbox = OutboxService(config, self.clock)
        self.alerts = AlertService(self.clock, self.outbox)
        self.commands = CommandService(config, self.clock, self.publisher, self.outbox)
        self.devices = DeviceService(config, self.clock, self.commands, self.outbox, self.alerts)
        self.credentials = CredentialService(self.clock, self.outbox)
        self.access = AccessService(config, self.clock, self.devices, self.commands, self.outbox)
        self.telemetry = TelemetryService(
            config, self.clock, self.devices, self.commands, self.outbox, self.alerts
        )
        self.forwarder = OutboxForwarder(config, self.clock, self.outbox, self.cloud)
        self.sync = SyncService(config, self.clock, self.outbox, self.cloud, self.alerts)

        self.router = MessageRouter(config.mqtt_topic_prefix, on_rejected=self._on_rejected)
        self.router.register("heartbeat", self.devices.on_heartbeat)
        self.router.register("access", self.access.on_access)
        self.router.register("readings", self.telemetry.on_reading)
        self.router.register("ack", self.commands.handle_ack)

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
        self.commands.check_timeouts()
        self.sync.check_staleness()

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

    def stop_background(self) -> None:
        for scheduler in self.schedulers:
            scheduler.stop()
