"""Commands sent to the nodes and the acknowledgements that confirm them."""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from ..clock import to_epoch
from ..mqtt import contract
from . import outbox as ob

logger = logging.getLogger(__name__)

LOCK_COMMANDS = ("access_result", "unlock")


def expects_lock_movement(type_: str, params: dict) -> bool:
    """True when the command asks the node to energise the lock."""
    return type_ in LOCK_COMMANDS and params.get("result", "GRANTED") == "GRANTED"


@dataclass
class Pending:
    command_id: str
    device_id: str
    type: str
    deadline: object
    lock: bool = False  # the lock is expected to move, so a missing ack is a lock failure
    waiting: bool = False  # a REST caller is waiting for the ack and owns the timeout
    event: threading.Event = field(default_factory=threading.Event)
    status: str | None = None


class PendingCommands:
    """Thread-safe registry of commands that still wait for an ack."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, Pending] = {}

    def add(self, pending: Pending) -> None:
        with self._lock:
            self._items[pending.command_id] = pending

    def resolve(self, command_id: str, status: str) -> Pending | None:
        with self._lock:
            pending = self._items.pop(command_id, None)
        if pending:
            pending.status = status
            pending.event.set()
        return pending

    def expired(self, now) -> list[Pending]:
        with self._lock:
            due = [p for p in self._items.values() if not p.waiting and p.deadline <= now]
            for p in due:
                del self._items[p.command_id]
        return due

    def discard(self, command_id: str) -> None:
        with self._lock:
            self._items.pop(command_id, None)

    def discard_device(self, device_id: str) -> int:
        with self._lock:
            ids = [c for c, p in self._items.items() if p.device_id == device_id]
            for c in ids:
                del self._items[c]
        return len(ids)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class CommandService:
    def __init__(self, config, clock, publisher, outbox):
        self.config = config
        self.clock = clock
        self.publisher = publisher
        self.outbox = outbox
        self.pending = PendingCommands()

    def send(self, device_id: str, type_: str, params: dict | None = None, *, waiting=False) -> Pending:
        """Publish a command and track its acknowledgement."""
        now = self.clock.now()
        command_id = uuid.uuid4().hex
        command = {
            "v": contract.SCHEMA_VERSION,
            "commandId": command_id,
            "type": type_,
            "params": params or {},
            "ts": to_epoch(now),
        }
        pending = Pending(
            command_id=command_id,
            device_id=device_id,
            type=type_,
            deadline=now + timedelta(seconds=self.config.ack_timeout_s),
            lock=expects_lock_movement(type_, params or {}),
            waiting=waiting,
        )
        self.pending.add(pending)
        try:
            self.publisher.publish_command(device_id, command)
        except Exception:
            self.pending.discard(command_id)
            raise
        return pending

    def send_and_wait(self, device_id: str, type_: str, params=None, timeout: float | None = None):
        """Send a command and block until its ack arrives. Returns the final status or None."""
        pending = self.send(device_id, type_, params, waiting=True)
        timeout = self.config.ack_timeout_s if timeout is None else timeout
        if pending.event.wait(timeout):
            return pending
        self.pending.discard(pending.command_id)
        return None

    def handle_ack(self, device_id: str, data: dict) -> None:
        pending = self.pending.resolve(data["commandId"], data["status"])
        if pending is None or pending.device_id != device_id:
            logger.debug("Ignoring ack for unknown command %s", data["commandId"])
            return
        if data["status"] == "FAILED":
            self.outbox.enqueue(
                ob.COMMAND_RESULT,
                {
                    "deviceId": device_id,
                    "commandId": pending.command_id,
                    "type": pending.type,
                    "status": "FAILED",
                    "detail": data.get("detail"),
                },
            )

    def check_timeouts(self, now=None) -> int:
        """Report commands whose ack never arrived. Returns how many expired."""
        now = now or self.clock.now()
        expired = self.pending.expired(now)
        for pending in expired:
            self.outbox.enqueue(
                ob.COMMAND_RESULT,
                {
                    "deviceId": pending.device_id,
                    "commandId": pending.command_id,
                    "type": pending.type,
                    "status": "TIMEOUT",
                },
            )
            if pending.lock:
                self.outbox.enqueue(
                    ob.ALERT,
                    {"alert": "lock_unresponsive", "state": "raised", "deviceId": pending.device_id},
                )
        return len(expired)
