from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import LoginState
from backend.intelligence.hirehi_category import HireHiCategoryChoice
from backend.orchestrator import workflow
from backend.persistence.database import Base
from backend.persistence.models import CandidateProfile, JobSession, Resume
from backend.schemas.domain import SessionStatus


class _Page:
    url = "https://example.test/search"

    def locator(self, _selector):
        return SimpleNamespace(inner_text=lambda: _empty_text())


async def _empty_text():
    return ""


class _Adapter:
    def __init__(self, site_id, refs=()):
        self.site_id = site_id
        self.refs = list(refs)
        self.filters = []

    async def get_login_state(self, page):
        return LoginState(authenticated=True, message="ok")

    async def open_search(self, page, filters):
        self.filters.append(filters)

    async def detect_blockers(self, page):
        return []

    async def collect_job_refs(self, page):
        return self.refs

    async def collect_more_job_refs(self, page):
        return []


@pytest.fixture
def search_runtime(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'search.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        profile = CandidateProfile(full_name="Test", gender="male", contacts={}, education=[], languages=[])
        db.add(profile)
        db.flush()
        db.add(Resume(profile_id=profile.id, name="Resume", desired_title="Python developer",
                      selected_for_matching=True))
        item = JobSession(profile_id=profile.id, adapter_id="test", status=SessionStatus.CREATED,
                          counters={})
        db.add(item)
        db.commit()
        session_id = item.id
    monkeypatch.setattr(workflow, "SessionLocal", sessions)
    monkeypatch.setattr(workflow, "get_browser", lambda _: SimpleNamespace(page=_Page()))
    return sessions, session_id


@pytest.mark.asyncio
async def test_hh_passes_only_planned_queries_and_emits_plan(search_runtime, monkeypatch):
    sessions, session_id = search_runtime
    adapter = _Adapter("hh")
    planner_calls = []

    async def planner(gateway, resumes, *, preference_policy=None):
        planner_calls.append((resumes, preference_policy))
        return ["backend engineer", "platform engineer"]

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "plan_search_queries", planner)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    await workflow.WorkflowManager()._run(session_id)

    assert adapter.filters == [{"queries": ["backend engineer", "platform engineer"]}]
    assert "Python developer" not in str(adapter.filters)
    assert len(planner_calls) == 1
    assert planner_calls[0][1] is None
    with sessions() as db:
        # Search-plan events are persisted in the shared events table.
        from backend.persistence.models import BrowserEvent
        events = list(db.scalars(select(BrowserEvent)).all())
        plan = next(e for e in events if e.event_type == "search_plan")
        assert plan.data["queries"] == ["backend engineer", "platform engineer"]


@pytest.mark.asyncio
async def test_hirehi_keeps_category_flow_without_search_planner(search_runtime, monkeypatch):
    sessions, session_id = search_runtime
    with sessions() as db:
        db.get(JobSession, session_id).adapter_id = "hirehi"
        db.commit()
    adapter = _Adapter("hirehi")

    async def unexpected_planner(*args):
        raise AssertionError("HireHi must not invoke search planner")

    async def category(*args):
        return HireHiCategoryChoice(category="менеджмент", reason="fixture")

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "plan_search_queries", unexpected_planner)
    monkeypatch.setattr(workflow, "choose_hirehi_category", category)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    await workflow.WorkflowManager()._run(session_id)
    assert adapter.filters and adapter.filters[0]["category"] == "менеджмент"
    assert "queries" not in adapter.filters[0]


@pytest.mark.asyncio
async def test_empty_plan_finishes_without_text_fallback(search_runtime, monkeypatch):
    sessions, session_id = search_runtime
    adapter = _Adapter("hh")
    async def empty_plan(*args, preference_policy=None):
        assert preference_policy is None
        return []
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "plan_search_queries", empty_plan)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    await workflow.WorkflowManager()._run(session_id)
    assert adapter.filters == [{"queries": []}]
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
