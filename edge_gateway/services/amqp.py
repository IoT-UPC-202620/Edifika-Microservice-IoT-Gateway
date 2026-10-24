"""AMQP event publisher (RabbitMQ) with publisher confirms."""

import json
import logging
import threading
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


class AmqpCommandConsumer:
    def __init__(self, config, handler, connection_factory=None):
        self.config = config
        self.handler = handler
        self.connection_factory = connection_factory or (
            lambda: pika.BlockingConnection(pika.URLParameters(self.config.amqp_url))
        )
        self._thread = None
        self._stopped = False
        self._connection = None
        self._channel = None

    def _setup(self, ch):
        queue_name = f"edge.{self.config.gateway_id}.commands"
        ch.queue_declare(
            queue=queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "edifika.dlx",
                "x-dead-letter-routing-key": queue_name,
            },
        )
        ch.queue_bind(
            queue=queue_name,
            exchange=self.config.amqp_commands_exchange,
            routing_key=f"{self.config.gateway_id}.#",
        )
        ch.basic_qos(prefetch_count=self.config.amqp_prefetch)
        ch.basic_consume(
            queue=queue_name,
            on_message_callback=self._on_message,
        )

    def _on_message(self, ch, method, properties, body):
        from ..db import database

        try:
            with database.connection_context():
                envelope = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
                self.handler.handle(envelope)
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as exc:
            logger.exception("Error processing command message %s: %s", getattr(method, "delivery_tag", None), exc)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start(self) -> None:
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="amqp-commands")
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        if self._channel is not None:
            try:
                self._channel.stop_consuming()
            except Exception:
                pass
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        import time

        backoff = 1
        while not self._stopped:
            try:
                self._connection = self.connection_factory()
                self._channel = self._connection.channel()
                self._setup(self._channel)
                backoff = 1
                self._channel.start_consuming()
            except Exception as exc:
                if self._stopped:
                    break
                logger.warning(
                    "AMQP consumer connection dropped: %s; reconnecting in %ds",
                    exc,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                if self._connection and not getattr(self._connection, "is_closed", False):
                    try:
                        self._connection.close()
                    except Exception:
                        pass
                self._connection = None
                self._channel = None

