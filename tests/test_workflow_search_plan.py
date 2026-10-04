from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import LoginState
from backend.intelligence.hirehi_category import HireHiCategoryChoice
from backend.orchestrator import workflow
from backend.orchestrator.search_version import HIREHI_SEARCH_ADAPTIVE_V3
from backend.persistence.database import Base
from backend.persistence.models import BrowserEvent, JobSession
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot


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
        item = JobSession(adapter_id="test", status=SessionStatus.CREATED,
                          counters={})
        db.add(item)
        db.flush()
        persist_session_snapshot(
            db, item.id,
            _normalize_extracted(
                {"external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                 "target": {"title": "Python developer"}, "about": "Fixture professional background",
                 "skills": [{"name": "Python"}]},
                adapter_id="test", source_url="https://test/resume/fixture",
            ),
        )
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
        item = db.get(JobSession, session_id)
        item.adapter_id = "hirehi"
        persist_session_snapshot(
            db,
            session_id,
            _normalize_extracted(
                {"external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                 "target": {"title": "Python developer"}, "about": "Fixture professional background",
                 "skills": [{"name": "Python"}]},
                adapter_id="hirehi", source_url="https://hirehi.ru/resume/fixture",
            ),
        )
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


async def _run_adaptive_restore_case(search_runtime, monkeypatch, *, corrupt: bool):
    sessions, session_id = search_runtime
    order = []
    planner_calls = []
    collected = []

    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hirehi"
        item.recovery = {
            "search_version": HIREHI_SEARCH_ADAPTIVE_V3,
            "search_checkpoint": {"corrupt": corrupt},
        }
        persist_session_snapshot(
            db,
            session_id,
            _normalize_extracted(
                {"external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                 "target": {"title": "Python developer"}, "about": "Fixture professional background",
                 "skills": [{"name": "Python"}]},
                adapter_id="hirehi", source_url="https://hirehi.ru/resume/fixture",
            ),
        )
        db.commit()

    class AdaptiveAdapter(_Adapter):
        collect_more_job_refs = None

        async def open_source(self, page, spec, cursor=0):
            return None

        async def collect_card_refs(self, page):
            return []

        async def collect_job_refs(self, page):
            collected.append("initial")
            return []

    raw_adapter = AdaptiveAdapter("hirehi")

    class FakeAdaptiveEngine:
        def __init__(self, adapter, *_args, **_kwargs):
            self.adapter = adapter
            self.portfolio = {}
            self.scheduler = SimpleNamespace(sources={})
            self.rejection_reasons = {}

        def __getattr__(self, name):
            return getattr(self.adapter, name)

        async def restore_search_checkpoint(self, checkpoint):
            order.append("restore")
            if checkpoint.get("corrupt"):
                raise ValueError("fixture checkpoint is corrupt")
            self.portfolio = {"saved": {"source_id": "saved"}}

        async def open_search(self, page, filters):
            order.append("open")
            if not self.portfolio:
                planner_calls.append("fresh")
                self.portfolio = {"fresh": {"source_id": "fresh"}}

        def search_checkpoint(self):
            return {
                "criteria_hash": "fresh" if planner_calls else "saved",
                "portfolio": list(self.portfolio),
            }

        def metrics(self):
            return {"D": 0, "N": 0, "R": 0}

        def audit_metrics(self):
            return {"eligible": 0, "selected": 0, "audited_relevant": 0, "fnr": 0.0}

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: raw_adapter)
    monkeypatch.setattr(workflow, "HireHiAdaptiveSearch", FakeAdaptiveEngine)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    metric_identities = []
    monkeypatch.setattr(
        workflow.search_metrics,
        "initialize",
        lambda _db, _item, _profile, _resumes, **kwargs: metric_identities.append(kwargs),
    )

    await workflow.WorkflowManager()._run(session_id)
    return sessions, session_id, order, planner_calls, collected, metric_identities


@pytest.mark.asyncio
async def test_hirehi_adaptive_restores_before_open_without_replanning(search_runtime, monkeypatch):
    sessions, session_id, order, planner_calls, collected, metric_identities = (
        await _run_adaptive_restore_case(search_runtime, monkeypatch, corrupt=False)
    )

    assert order == ["restore", "open"]
    assert planner_calls == []
    assert collected == []
    assert metric_identities[-1]["criteria_hash"] == "saved"
    with sessions() as db:
        checkpoint = db.get(JobSession, session_id).recovery["search_checkpoint"]
        assert checkpoint["portfolio"] == ["saved"]
        assert not db.scalar(
            select(BrowserEvent).where(
                BrowserEvent.session_id == session_id,
                BrowserEvent.event_type == "checkpoint_discarded",
            )
        )


@pytest.mark.asyncio
async def test_hirehi_adaptive_corrupt_checkpoint_falls_back_to_one_fresh_plan(search_runtime, monkeypatch):
    sessions, session_id, order, planner_calls, collected, metric_identities = (
        await _run_adaptive_restore_case(search_runtime, monkeypatch, corrupt=True)
    )

    assert order == ["restore", "open"]
    assert planner_calls == ["fresh"]
    assert collected == ["initial"]
    assert metric_identities[-1]["criteria_hash"] == "fresh"
    with sessions() as db:
        event = db.scalar(
            select(BrowserEvent).where(
                BrowserEvent.session_id == session_id,
                BrowserEvent.event_type == "checkpoint_discarded",
            )
        )
        assert event is not None
        assert event.data["kind"] == "invalid_adaptive_checkpoint"
