"""Small, typed and bounded messages exchanged with a site worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from multiprocessing.queues import Queue
from queue import Empty, Full
from typing import Any, Literal

CommandName = Literal[
    "START", "STOP", "PAUSE", "RESUME", "OPEN_BROWSER", "CHECK_LOGIN", "PREVIEW", "REFRESH",
]
EventName = Literal[
    "HEARTBEAT", "PROGRESS", "READY", "PAUSED", "STOPPED", "COMPLETED", "FAILED",
    "LOGIN_REQUIRED", "COMMAND_RESULT",
]


def _check_payload(payload: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
    if not isinstance(payload, dict) or len(payload) > 32:
        raise ValueError("worker payload must be a small mapping")
    result: dict[str, str | int | float | bool | None] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or len(key) > 120:
            raise ValueError("worker payload keys must be short strings")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("worker IPC carries identifiers/commands, not document data")
        if isinstance(value, str) and len(value) > 4096:
            raise ValueError("worker payload value is too large")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class WorkerCommand:
    session_id: int
    generation: int
    command: CommandName
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.session_id <= 0 or self.generation < 0:
            raise ValueError("invalid worker command identity")
        _check_payload(self.payload)


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    session_id: int
    generation: int
    event: EventName
    stage: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.session_id <= 0 or self.generation < 0:
            raise ValueError("invalid worker event identity")
        _check_payload(self.payload)


class BoundedChannel:
    """Queue wrapper that never grows without limit or blocks the API."""

    def __init__(self, queue: Queue, *, capacity: int) -> None:
        self.queue = queue
        self.capacity = capacity

    def send(self, message: WorkerCommand | WorkerEvent) -> bool:
        try:
            self.queue.put_nowait(message)
        except Full:
            return False
        return True

    def receive(self) -> WorkerCommand | WorkerEvent | None:
        try:
            return self.queue.get_nowait()
        except Empty:
            return None
