"""AMQP event publisher (RabbitMQ) with publisher confirms."""

import json
import logging
import pika
import pika.exceptions

from .transport import CloudRejected, CloudUnavailable

logger = logging.getLogger(__name__)


def _kind_to_pascal(kind: str) -> str:
    return "".join(part.capitalize() for part in kind.split("_"))


def to_envelope(event: dict, gateway_code: str) -> tuple[str, bytes, dict]:
    try:
        kind = event.get("kind", "")
        event_type = _kind_to_pascal(kind)
        routing_key = f"edge.{kind.replace('_', '-')}"
        payload = event.get("payload", {})
        correlation_id = payload.get("commandId") if kind == "command_result" else None

        envelope = {
            "eventId": event["eventId"],
            "eventType": event_type,
            "eventVersion": 1,
            "occurredAt": event["occurredAt"],
            "producer": "edge-gateway",
            "buildingId": None,
            "gatewayCode": gateway_code,
            "correlationId": correlation_id,
            "payload": payload,
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    except Exception as exc:
        raise CloudRejected(f"Serialization failed: {exc}") from exc

    props = {
        "message_id": event["eventId"],
        "type": event_type,
        "content_type": "application/json",
        "content_encoding": "utf-8",
        "delivery_mode": 2,
        "headers": {"x-producer": "edge-gateway"},
    }
    if correlation_id is not None:
        props["correlation_id"] = correlation_id

    return routing_key, body, props


class AmqpEventPublisher:
    def __init__(self, config, connection_factory=None):
        self.config = config
        self.connection_factory = connection_factory or (
            lambda: pika.BlockingConnection(pika.URLParameters(self.config.amqp_url))
        )
        self._connection = None
        self._channel = None

    def _ensure_channel(self):
        if self._connection is None or getattr(self._connection, "is_closed", False):
            self._connection = self.connection_factory()
            self._channel = None
        if self._channel is None or getattr(self._channel, "is_closed", False):
            self._channel = self._connection.channel()
            self._channel.confirm_delivery()
        return self._channel

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None

    def post_events(self, events: list[dict]) -> list[str]:
        if not events:
            return []

        # Validate/serialize all envelopes first to detect any CloudRejected before publishing
        envelopes = []
        for event in events:
            rk, body, props = to_envelope(event, self.config.gateway_id)
            envelopes.append((event["eventId"], rk, body, pika.BasicProperties(**props)))

        confirmed: list[str] = []
        for idx, (event_id, rk, body, basic_props) in enumerate(envelopes):
            try:
                ch = self._ensure_channel()
                ch.basic_publish(
                    exchange=self.config.amqp_events_exchange,
                    routing_key=rk,
                    body=body,
                    properties=basic_props,
                    mandatory=True,
                )
                confirmed.append(event_id)
            except Exception as exc:
                logger.warning("AMQP publish failed for event %s: %s", event_id, exc)
                self.close()
                if idx == 0:
                    raise CloudUnavailable(str(exc)) from exc
                return confirmed

        return confirmed
