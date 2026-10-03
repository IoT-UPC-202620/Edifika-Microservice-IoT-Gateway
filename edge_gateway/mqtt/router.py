"""Routes validated inbound messages to the service handlers."""

import logging
from collections import Counter

from ..db import database
from . import contract

logger = logging.getLogger(__name__)


class MessageRouter:
    """Validates a raw MQTT message and hands it to the handler of its channel.

    A malformed message is discarded and counted; it never stops the ingestion
    of the messages that follow.
    """

    def __init__(self, prefix: str, on_rejected=None):
        self.prefix = prefix
        self.handlers: dict = {}
        self.on_rejected = on_rejected
        self.rejected = Counter()

    def register(self, channel: str, handler) -> None:
        self.handlers[channel] = handler

    def handle(self, topic: str, raw: bytes | str) -> bool:
        """Process one message. Returns True when a handler consumed it."""
        try:
            device_id, channel, data = contract.parse_message(self.prefix, topic, raw)
        except contract.ContractError as err:
            self.rejected[err.code] += 1
            logger.warning("Rejected message on %s: %s (%s)", topic, err.message, err.code)
            if self.on_rejected:
                self._safe(self.on_rejected, err)
            return False
        handler = self.handlers.get(channel)
        if handler is None:
            return False
        with database.connection_context():
            try:
                handler(device_id, data)
            except Exception:
                logger.exception("Handler for %s failed for device %s", channel, device_id)
                return False
        return True

    @staticmethod
    def _safe(callback, err) -> None:
        try:
            with database.connection_context():
                callback(err)
        except Exception:
            logger.exception("Rejection callback failed")
