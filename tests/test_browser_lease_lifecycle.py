from types import SimpleNamespace

import pytest

from backend.api import router
from backend.orchestrator import workflow
from backend.schemas.domain import SessionStatus


class _DB:
    def __init__(self, item):
        self.item = item

    def get(self, model, session_id):
        return self.item

    def scalar(self, statement):
        # This double represents a legacy session with no resume snapshot.
        return None

    def scalars(self, statement):
        return iter(())

    def commit(self):
        pass

    def refresh(self, item):
        pass


class _Context:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        return False


def _session(status=SessionStatus.RUNNING, adapter_id="hh"):
    return SimpleNamespace(
        id=1,
        adapter_id=adapter_id,
        status=status,
        stop_reason=None,
        finished_at=None,
        counters={},
        recovery={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, closes",
    [
        (SessionStatus.PAUSED, False),
        (SessionStatus.COMPLETED, True),
        (SessionStatus.STOPPED, True),
        (SessionStatus.FAILED, True),
    ],
)
async def test_workflow_run_closes_browser_only_for_terminal_status(monkeypatch, status, closes):
    monkeypatch.setattr(workflow.search_metrics, "freeze", lambda db, item: None)
    item = _session()
    db = _DB(item)
    closed = []

    async def fake_close(session_id):
        closed.append(session_id)

    async def fake_run(self, session_id):
        item.status = status

    monkeypatch.setattr(workflow, "SessionLocal", lambda: _Context(db))
    monkeypatch.setattr(workflow, "close_browser", fake_close)
    monkeypatch.setattr(workflow.WorkflowManager, "_run", fake_run)

    manager = workflow.WorkflowManager()
    manager.task_sites[1] = "hh"
    await manager.run(1)

    assert closed == ([1] if closes else [])
    assert 1 not in manager.task_sites
    assert "hh" not in manager.site_leases


@pytest.mark.asyncio
async def test_open_session_browser_releases_lease_when_start_fails(monkeypatch):
    item = _session(SessionStatus.CREATED, "hh")
    db = _DB(item)
    adapter = SimpleNamespace(site_id="hh", allowed_domains=["hh.ru"], display_name="HH")
    released = []

    class FailingExecutor:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("start failed")

    monkeypatch.setattr(router, "get_browser", lambda session_id: None)
    monkeypatch.setattr(router.adapter_registry, "get", lambda adapter_id: adapter)
    monkeypatch.setattr(router, "acquire_browser_lease", lambda *args: True)
    monkeypatch.setattr(router, "release_browser_lease", lambda *args: released.append(args))
    monkeypatch.setattr(router, "BrowserExecutor", FailingExecutor)

    with pytest.raises(RuntimeError, match="start failed"):
        await router.open_session_browser(1, db)

    assert released == [(1, "hh")]


@pytest.mark.asyncio
async def test_open_browser_routes_to_active_worker_without_creating_api_context(monkeypatch):
    item = _session(SessionStatus.RUNNING, "hh")
    db = _DB(item)
    adapter = SimpleNamespace(site_id="hh", allowed_domains=["hh.ru"], display_name="HH")
    worker = SimpleNamespace(session_id=1, process=SimpleNamespace(is_alive=lambda: True))
    calls = []

    class Supervisor:
        def worker(self, site_id):
            assert site_id == "hh"
            return worker

        async def open_browser(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "message": "Открыто окно браузера сессии"}

    monkeypatch.setattr(router, "runtime_supervisor", Supervisor())
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter_id: adapter)
    monkeypatch.setattr(router, "get_browser", lambda _session_id: pytest.fail("API registry was consulted"))
    monkeypatch.setattr(router, "BrowserExecutor", lambda *_args, **_kwargs: pytest.fail("duplicate context created"))

    result = await router.open_session_browser(1, db)

    assert result["ok"] is True
    assert calls == [{"site_id": "hh", "session_id": 1}]


@pytest.mark.asyncio
async def test_open_browser_reports_worker_failure_instead_of_claiming_success(monkeypatch):
    item = _session(SessionStatus.RUNNING, "hh")
    db = _DB(item)
    adapter = SimpleNamespace(site_id="hh", allowed_domains=["hh.ru"], display_name="HH")
    worker = SimpleNamespace(session_id=1, process=SimpleNamespace(is_alive=lambda: True))

    class Supervisor:
        def worker(self, _site_id):
            return worker

        async def open_browser(self, **_kwargs):
            return {"ok": False, "message": "Браузер сессии ещё не готов"}

    monkeypatch.setattr(router, "runtime_supervisor", Supervisor())
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter_id: adapter)
    monkeypatch.setattr(router, "BrowserExecutor", lambda *_args, **_kwargs: pytest.fail("duplicate context created"))

    with pytest.raises(router.HTTPException) as error:
        await router.open_session_browser(1, db)
    assert error.value.status_code == 503
    assert error.value.detail == "Браузер сессии ещё не готов"


@pytest.mark.asyncio
async def test_active_worker_login_status_uses_worker_ack(monkeypatch):
    item = _session(SessionStatus.RUNNING, "hh")
    db = _DB(item)
    adapter = SimpleNamespace(site_id="hh", allowed_domains=["hh.ru"], display_name="HH")
    worker = SimpleNamespace(session_id=1, process=SimpleNamespace(is_alive=lambda: True))
    calls = []

    class Supervisor:
        def worker(self, _site_id):
            return worker

        async def check_login(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "authenticated": True, "message": "Вход подтверждён", "url": "https://hh.ru/"}

    monkeypatch.setattr(router, "runtime_supervisor", Supervisor())
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter_id: adapter)
    monkeypatch.setattr(router, "get_browser", lambda _session_id: pytest.fail("API registry was consulted"))

    result = await router.session_browser_login_status(1, db)

    assert result == {"authenticated": True, "message": "Вход подтверждён", "url": "https://hh.ru/"}
    assert calls == [{"site_id": "hh", "session_id": 1}]


@pytest.mark.asyncio
async def test_stop_session_closes_browser(monkeypatch):
    item = _session()
    db = _DB(item)
    closed = []
    monkeypatch.setattr(router.workflow_manager, "write_hirehi_report", lambda session_id: 0)
    monkeypatch.setattr(router, "session_dict", lambda current: {"status": current.status})

    async def close(session_id):
        closed.append(session_id)

    monkeypatch.setattr(router, "close_browser", close)
    result = await router.stop_session(1, db)

    assert result["status"] == SessionStatus.STOPPED
    assert closed == [1]
