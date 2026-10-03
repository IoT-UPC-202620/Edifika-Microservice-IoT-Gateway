"""Runtime configuration for the Edge Gateway, loaded from environment variables."""

import os
from dataclasses import dataclass, fields


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
    cloud_token: str = ""

    # Persistence
    database_path: str = "data/edge-gateway.db"

    # MQTT (local broker shared with the ESP32 nodes)
    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "edifika/v1"

    # Cloud backend
    cloud_base_url: str = ""
    cloud_timeout_s: float = 5.0

    # Behaviour
    heartbeat_timeout_s: int = 30
    ack_timeout_s: float = 3.0
    dedupe_window_s: float = 2.0
    lock_open_ms: int = 5000
    clock_drift_s: int = 2
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
        env = os.environ if env is None else env
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
