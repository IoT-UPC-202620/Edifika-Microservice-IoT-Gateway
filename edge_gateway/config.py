"""Runtime configuration for the Edge Gateway, loaded from environment variables."""

import logging
import os
from dataclasses import dataclass, fields

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid."""


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # Identity
    gateway_id: str = "edge-gateway-01"
    timezone: str = "America/Lima"

    # Security: bearer token required by the local REST API (cloud and admin callers)
    service_token: str = ""

    # Persistence
    database_path: str = "data/edge-gateway.db"

    # MQTT (local broker shared with the ESP32 nodes)
    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "edifika/v1"
    mqtt_tls: bool = False
    mqtt_ca_file: str = ""
    mqtt_client_id: str = ""

    # AMQP broker
    amqp_url: str = ""
    amqp_events_exchange: str = "edifika.events"
    amqp_commands_exchange: str = "edifika.edge.commands"
    amqp_prefetch: int = 10

    # Sync service (IoT Access Management)
    sync_base_url: str = ""
    sync_api_key: str = ""
    cloud_timeout_s: float = 5.0

    # Behaviour
    heartbeat_timeout_s: int = 30
    ack_timeout_s: float = 3.0
    remote_command_timeout_s: float = 5.0
    dedupe_window_s: float = 2.0
    lock_open_ms: int = 5000
    clock_drift_s: int = 2
    mode_resync_interval_s: float = 10.0  # min seconds between two mode re-sends to the same node
    max_maintenance_minutes: int = 240
    cache_max_age_s: int = 24 * 3600

    # Cloud forwarding and synchronization
    outbox_batch_size: int = 50
    outbox_max_rows: int = 10000
    outbox_retention_s: int = 24 * 3600
    outbox_interval_s: float = 5.0
    sync_interval_s: float = 60.0

    # Background workers (disabled in tests)
    background_enabled: bool = True

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = dict(os.environ if env is None else env)
        if "EDGE_CLOUD_BASE_URL" in env and "EDGE_SYNC_BASE_URL" not in env:
            logger.warning("EDGE_CLOUD_BASE_URL is deprecated; use EDGE_SYNC_BASE_URL")
            env["EDGE_SYNC_BASE_URL"] = env["EDGE_CLOUD_BASE_URL"]
        if "EDGE_CLOUD_TOKEN" in env and "EDGE_SYNC_API_KEY" not in env:
            logger.warning("EDGE_CLOUD_TOKEN is deprecated; use EDGE_SYNC_API_KEY")
            env["EDGE_SYNC_API_KEY"] = env["EDGE_CLOUD_TOKEN"]

        values = {}
        for f in fields(cls):
            raw = env.get("EDGE_" + f.name.upper())
            if raw is None:
                continue
            try:
                if f.type is bool:
                    values[f.name] = _bool(raw)
                elif f.type is int:
                    values[f.name] = int(raw)
                elif f.type is float:
                    values[f.name] = float(raw)
                else:
                    values[f.name] = raw
            except ValueError as exc:
                raise ConfigError(f"Invalid value for EDGE_{f.name.upper()}: {raw!r}") from exc
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        if not self.service_token:
            raise ConfigError("EDGE_SERVICE_TOKEN is required")
        if self.mqtt_enabled and not self.mqtt_host:
            raise ConfigError("EDGE_MQTT_HOST is required when MQTT is enabled")
