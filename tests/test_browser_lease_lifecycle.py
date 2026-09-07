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

    def commit(self):
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
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, closes",
    [
        (SessionStatus.PAUSED, False),
        (SessionStatus.WAITING_FOR_LOGIN, False),
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
    item = _session(SessionStatus.WAITING_FOR_LOGIN, "hh")
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
