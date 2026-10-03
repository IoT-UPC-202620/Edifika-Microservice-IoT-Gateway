"""Small periodic task runner for the gateway's background work."""

import logging
import threading
import time

from .db import database

logger = logging.getLogger(__name__)


class Scheduler:
    """Runs named tasks at fixed intervals in a single daemon thread.

    One failing task never stops the others; every run gets its own database connection.
    """

    def __init__(self, tasks, tick_s: float = 0.5, monotonic=time.monotonic):
        self.tasks = [{"name": n, "interval": i, "fn": f, "next": 0.0} for n, i, f in tasks]
        self.tick_s = tick_s
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="edge-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_due(self) -> int:
        """Run every task whose interval elapsed. Returns how many ran."""
        now = self._monotonic()
        ran = 0
        for task in self.tasks:
            if now < task["next"]:
                continue
            task["next"] = now + task["interval"]
            ran += 1
            try:
                with database.connection_context():
                    task["fn"]()
            except Exception:
                logger.exception("Scheduled task %s failed", task["name"])
        return ran

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.run_due()
