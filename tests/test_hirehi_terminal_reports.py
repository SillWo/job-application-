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

    def commit(self):
        self.commits += 1

    def add(self, value):
        return None


def session(adapter_id):
    return SimpleNamespace(
        id=7, profile_id=1, adapter_id=adapter_id, status=SessionStatus.RUNNING,
        stop_reason=None, finished_at=None, counters={},
        minimum_scores=None, application_limit=1,
        started_at=None,
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


def test_finalize_stopped_hirehi_keeps_status_and_reports(monkeypatch):
    item = session("hirehi")
    item.status = SessionStatus.STOPPED
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
