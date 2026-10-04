import asyncio
from queue import Empty
from types import SimpleNamespace

import pytest

from backend.browser import sessions as browser_sessions
from backend.persistence.models import JobSession
from backend.runtime import worker
from backend.runtime.ipc import BoundedChannel, WorkerCommand, WorkerEvent
from backend.schemas.domain import SessionStatus


class _Context:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_args):
        return False


class _DB:
    def __init__(self, item, execution):
        self.item = item
        self.execution = execution

    def get(self, model, _session_id):
        if model is JobSession:
            return self.item
        return None

    def scalar(self, _statement):
        return self.execution


class _Events:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return True


class _Executor:
    def __init__(self):
        self.page = SimpleNamespace(url="https://hh.ru/vacancy/123")
        self.front_count = 0

    async def bring_to_front(self):
        self.front_count += 1


@pytest.fixture
def runtime_state(monkeypatch):
    item = SimpleNamespace(id=12, adapter_id="hh", status=SessionStatus.RUNNING)
    execution = SimpleNamespace(session_id=12, generation=7, cancel_requested=False)
    db = _DB(item, execution)
    events = _Events()
    monkeypatch.setattr("backend.persistence.database.SessionLocal", lambda: _Context(db))
    monkeypatch.setattr("backend.runtime.lifecycle.cancellation_fence", lambda *_args: False)
    return db, events


@pytest.mark.asyncio
async def test_open_command_focuses_existing_worker_page_without_navigation(monkeypatch, runtime_state):
    _db, events = runtime_state
    executor = _Executor()
    previous = browser_sessions.open_browsers.get(12)
    browser_sessions.open_browsers[12] = executor
    command = WorkerCommand(12, 7, "OPEN_BROWSER", {"command_id": "open-1"})
    try:
        await worker._handle_open_browser_command(
            command, events, session_id=12, generation=7,
        )
    finally:
        if previous is None:
            browser_sessions.open_browsers.pop(12, None)
        else:
            browser_sessions.open_browsers[12] = previous

    assert executor.front_count == 1
    assert executor.page.url == "https://hh.ru/vacancy/123"
    assert events.messages == [
        WorkerEvent(
            12, 7, "COMMAND_RESULT", message="Открыто окно браузера сессии",
            payload={"command_id": "open-1", "ok": True, "authenticated": False, "url": None},
        )
    ]


@pytest.mark.asyncio
async def test_open_command_reports_browser_not_ready(runtime_state):
    _db, events = runtime_state
    previous = browser_sessions.open_browsers.pop(12, None)
    try:
        await worker._handle_open_browser_command(
            WorkerCommand(12, 7, "OPEN_BROWSER", {"command_id": "open-2"}),
            events, session_id=12, generation=7,
        )
    finally:
        if previous is not None:
            browser_sessions.open_browsers[12] = previous

    assert events.messages[0].payload["ok"] is False
    assert events.messages[0].message == "Браузер сессии ещё не готов"


@pytest.mark.asyncio
async def test_stale_open_command_is_rejected_before_browser_access(monkeypatch, runtime_state):
    db, events = runtime_state
    db.execution.generation = 8
    touched = []

    async def bring_to_front(_session_id):
        touched.append(True)

    monkeypatch.setattr(browser_sessions, "bring_browser_to_front", bring_to_front)
    await worker._handle_open_browser_command(
        WorkerCommand(12, 7, "OPEN_BROWSER", {"command_id": "open-stale"}),
        events, session_id=12, generation=7,
    )

    assert touched == []
    assert events.messages[0].payload["ok"] is False
    assert events.messages[0].message == "Запрос открытия браузера устарел"


def test_command_result_ack_is_correlated_to_request_waiter():
    from backend.runtime.supervisor import RuntimeSupervisor, WorkerHandle

    class Process:
        def is_alive(self):
            return True

    class Channel:
        def __init__(self):
            self.items = []

        def put_nowait(self, message):
            self.items.append(message)

        def get_nowait(self):
            if not self.items:
                raise Empty
            return self.items.pop(0)

    supervisor = RuntimeSupervisor(context="spawn")
    events = Channel()
    command_queue = Channel()
    handle = WorkerHandle(
        "hh", 12, 7, Process(), BoundedChannel(command_queue, capacity=8),
        BoundedChannel(events, capacity=8),
    )
    supervisor.workers["hh"] = handle

    async def exercise():
        request = asyncio.create_task(supervisor.open_browser(site_id="hh", session_id=12, timeout=1))
        await asyncio.sleep(0)
        sent = command_queue.items[0]
        events.put_nowait(WorkerEvent(
            12, 7, "COMMAND_RESULT", message="Открыто окно браузера сессии",
            payload={"command_id": sent.payload["command_id"], "ok": True},
        ))
        supervisor.poll("hh")
        return await request

    assert asyncio.run(exercise()) == {
        "ok": True,
        "message": "Открыто окно браузера сессии",
        "authenticated": False,
        "url": None,
    }


@pytest.mark.asyncio
async def test_stalled_page_returns_bounded_open_ack(monkeypatch, runtime_state):
    _db, events = runtime_state
    executor = _Executor()

    async def stall():
        await asyncio.sleep(1)

    executor.bring_to_front = stall
    previous = browser_sessions.open_browsers.get(12)
    browser_sessions.open_browsers[12] = executor
    monkeypatch.setattr(worker, "BROWSER_COMMAND_TIMEOUT", 0.01)
    try:
        await asyncio.wait_for(
            worker._handle_open_browser_command(
                WorkerCommand(12, 7, "OPEN_BROWSER", {"command_id": "open-timeout"}),
                events, session_id=12, generation=7,
            ),
            timeout=0.2,
        )
    finally:
        if previous is None:
            browser_sessions.open_browsers.pop(12, None)
        else:
            browser_sessions.open_browsers[12] = previous

    assert events.messages[0].payload["ok"] is False
    assert events.messages[0].message == "Открытие браузера заняло слишком много времени"
