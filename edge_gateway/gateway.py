"""Composition root: builds the services and wires their dependencies."""

from .clock import SystemClock
from .services.outbox import OutboxService


class Gateway:
    def __init__(self, config, clock=None):
        self.config = config
        self.clock = clock or SystemClock()
        self.outbox = OutboxService(config, self.clock)
