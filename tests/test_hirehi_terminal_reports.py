from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.api import router
from backend.orchestrator import workflow
from backend.schemas.domain import SessionStatus


class DB:
    def __init__(self, item):
        self.item = item
        self.commits = 0

    def get(self, model, ident):
        return self.item

    def scalar(self, statement):
        # This double represents a legacy session with no resume snapshot.
        return None

    def scalars(self, statement):
        return iter(())

    def commit(self):
        self.commits += 1

    def add(self, value):
        return None

    def refresh(self, item):
        pass


class DurableDB(DB):
    def flush(self):
        pass


def session(adapter_id):
    return SimpleNamespace(
        id=7, adapter_id=adapter_id, status=SessionStatus.RUNNING,
        stop_reason=None, finished_at=None, counters={},
        minimum_scores=None, application_limit=1,
        started_at=None, guaranteed_application=False,
        recovery={},
    )


@pytest.mark.asyncio
async def test_stop_hirehi_triggers_report(monkeypatch):
    item = session("hirehi")
    db = DB(item)
    calls = []
    monkeypatch.setattr(router.workflow_manager, "write_hirehi_report", calls.append)

    result = await router.stop_session(7, db)

    assert result["status"] == SessionStatus.STOPPED
    assert item.stop_reason == "Остановлено пользователем"
    assert isinstance(item.finished_at, datetime)
    assert calls == [7]


@pytest.mark.asyncio
async def test_stop_hh_does_not_trigger_report(monkeypatch):
    item = session("hh")
    db = DB(item)
    calls = []
    monkeypatch.setattr(router.workflow_manager, "write_hirehi_report", calls.append)

    await router.stop_session(7, db)

    assert item.status == SessionStatus.STOPPED
    assert calls == []


@pytest.mark.asyncio
async def test_durable_stop_without_runtime_worker_finalizes_cancelled(monkeypatch):
    item = session("hirehi")
    db = DurableDB(item)
    calls = []
    closed = []

    class Manager:
        tasks = {}

        def write_hirehi_report(self, session_id):
            calls.append(session_id)

        def _terminalize_pending_vacancies(self, current_db, current_item):
            return 0

    monkeypatch.setattr(router, "workflow_manager", Manager())
    monkeypatch.setattr(router.runtime_supervisor, "cancel", lambda **kwargs: False)
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter: type("Adapter", (), {"site_id": "hirehi"})())
    monkeypatch.setattr(router, "release_site_lease", lambda *args: False)
    monkeypatch.setattr(router, "session_dict", lambda current: {"status": current.status})

    async def close(session_id):
        closed.append(session_id)

    monkeypatch.setattr(router, "close_browser", close)
    result = await router.stop_session(7, db)

    assert result["status"] == SessionStatus.CANCELLED
    assert item.status == SessionStatus.CANCELLED
    assert item.stop_reason == "Остановлено пользователем"
    assert isinstance(item.finished_at, datetime)
    assert calls == [7]
    assert closed == [7]


@pytest.mark.asyncio
async def test_live_runtime_stop_returns_durable_stopping_without_local_cleanup(monkeypatch):
    item = session("hirehi")
    db = DurableDB(item)
    closed = []
    monkeypatch.setattr(router.runtime_supervisor, "cancel", lambda **kwargs: True)
    monkeypatch.setattr(router.adapter_registry, "get", lambda _adapter: type("Adapter", (), {"site_id": "hirehi"})())
    monkeypatch.setattr(router, "session_dict", lambda current: {"status": current.status})

    async def close(session_id):
        closed.append(session_id)

    monkeypatch.setattr(router, "close_browser", close)
    result = await router.stop_session(7, db)

    assert result["status"] == SessionStatus.STOPPING
    assert item.status == SessionStatus.STOPPING
    assert closed == []


def test_finalize_stopped_hirehi_keeps_status_and_reports(monkeypatch):
    item = session("hirehi")
    item.status = SessionStatus.STOPPED
    item.recovery = {"pending_refs": [{"external_id": "fixture", "url": "https://hirehi.ru/fixture"}]}
    item.stop_reason = "Остановлено пользователем"
    db = DB(item)
    calls = []

    class Context:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(workflow, "SessionLocal", lambda: Context())
    monkeypatch.setattr(workflow.WorkflowManager, "_write_hirehi_report", lambda self, current_db, ident: calls.append(ident))

    workflow.WorkflowManager().finalize(7, "завершено")

    assert item.status == SessionStatus.STOPPED
    assert item.stop_reason == "Остановлено пользователем"
    assert calls == [7]
