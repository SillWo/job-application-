"""A missing vacancy body is a typed extraction failure, not model input."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from test_workflow_non_captcha_continuation import FakeAdapter, evaluation
from test_workflow_non_captcha_continuation import runtime as base_runtime

from backend.adapters.base.errors import JobDescriptionUnavailable
from backend.adapters.base.protocol import Blocker, JobRef
from backend.adapters.hh import locators as hh_locators
from backend.adapters.hh.adapter import HHAdapter
from backend.adapters.hirehi import locators as hirehi_locators
from backend.adapters.hirehi.adapter import HireHiAdapter
from backend.adapters.zarplata import locators as zarplata_locators
from backend.adapters.zarplata.adapter import ZarplataAdapter
from backend.orchestrator import workflow
from backend.persistence.models import BrowserEvent, JobSession, Vacancy
from backend.persistence.pipeline_models import PipelineItem
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot

workflow_runtime = base_runtime


class _Text:
    def __init__(self, value="", *, error=None):
        self.value = value
        self.error = error
        self.first = self

    async def count(self):
        return int(self.value is not None or self.error is not None)

    def nth(self, index):
        return self

    async def is_visible(self):
        return True

    async def inner_text(self, **_kwargs):
        if self.error:
            raise self.error
        return self.value


class _Many:
    def __init__(self, values):
        self.items = [_Text(value, error=error) for value, error in values]

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    @property
    def first(self):
        return self.items[0] if self.items else _Text(None)


class _Page:
    def __init__(self, values, url="https://hh.ru/vacancy/123"):
        self.values = values
        self.url = url

    def locator(self, selector):
        value = self.values.get(selector)
        if isinstance(value, list):
            return _Many(value)
        return _Text(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter,locators,url",
    [
        (HHAdapter, hh_locators, "https://hh.ru/vacancy/123"),
        (ZarplataAdapter, zarplata_locators, "https://krasnoyarsk.zarplata.ru/vacancy/123"),
    ],
)
@pytest.mark.parametrize("description", [None, "", " \n\t "])
async def test_hh_and_zarplata_type_missing_or_blank_description(adapter, locators, url, description):
    with pytest.raises(JobDescriptionUnavailable) as caught:
        await adapter().extract_job(
            _Page(
                {
                    locators.VACANCY_TITLE: "Backend разработчик",
                    locators.COMPANY: "Компания",
                    locators.DESCRIPTION: description,
                },
                url,
            )
        )
    assert caught.value.title == "Backend разработчик"
    assert caught.value.company == "Компания"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter,locators,url",
    [
        (HHAdapter, hh_locators, "https://hh.ru/vacancy/123"),
        (ZarplataAdapter, zarplata_locators, "https://krasnoyarsk.zarplata.ru/vacancy/123"),
    ],
)
async def test_unreadable_first_description_candidate_does_not_hide_later_text(adapter, locators, url):
    page = _Page(
        {
            locators.VACANCY_TITLE: "Backend разработчик",
            locators.COMPANY: "Компания",
            locators.DESCRIPTION: [(None, RuntimeError("detached")), ("Нужное описание", None)],
        },
        url,
    )
    posting = await adapter().extract_job(page)
    assert posting.description == "Нужное описание"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter,locators,url",
    [
        (HHAdapter, hh_locators, "https://hh.ru/vacancy/123"),
        (ZarplataAdapter, zarplata_locators, "https://krasnoyarsk.zarplata.ru/vacancy/123"),
    ],
)
async def test_unreadable_description_is_typed_but_other_required_field_errors_are_not(adapter, locators, url):
    good_fields = {
        locators.VACANCY_TITLE: "Backend разработчик",
        locators.COMPANY: "Компания",
    }
    with pytest.raises(JobDescriptionUnavailable):
        await adapter().extract_job(
            _Page({**good_fields, locators.DESCRIPTION: [(None, RuntimeError("detached"))]}, url)
        )

    with pytest.raises(ValueError) as caught:
        await adapter().extract_job(_Page({locators.DESCRIPTION: "Описание"}, url))
    assert not isinstance(caught.value, JobDescriptionUnavailable)


class _HireHiPage:
    url = "https://hirehi.ru/vacancies/backend-123"

    def __init__(self, values):
        self.values = values

    def locator(self, selector):
        if selector in self.values:
            value = self.values[selector]
            return value if isinstance(value, _Text) else _Text(value)
        return _Text(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [None, "", " \n\t "])
async def test_hirehi_types_missing_or_blank_description(description):
    with pytest.raises(JobDescriptionUnavailable) as caught:
        await HireHiAdapter().extract_job(
            _HireHiPage(
                {
                    "h1": "Backend разработчик",
                    hirehi_locators.VACANCY_COMPANY.split(",")[0].strip(): "Компания",
                    hirehi_locators.VACANCY_DESCRIPTION.split(",")[0].strip(): description,
                    "main": "",
                }
            )
        )
    assert caught.value.title == "Backend разработчик"
    assert caught.value.company == "Компания"


@pytest.mark.asyncio
async def test_hirehi_non_description_required_field_error_remains_ordinary_value_error():
    with pytest.raises(ValueError) as caught:
        await HireHiAdapter().extract_job(_HireHiPage({"main": "Описание"}))
    assert not isinstance(caught.value, JobDescriptionUnavailable)


@pytest.mark.asyncio
async def test_hirehi_unreadable_description_is_typed_with_title_and_company():
    with pytest.raises(JobDescriptionUnavailable) as caught:
        await HireHiAdapter().extract_job(
            _HireHiPage(
                {
                    "h1": "Backend разработчик",
                    hirehi_locators.VACANCY_COMPANY.split(",")[0].strip(): "Компания",
                    hirehi_locators.VACANCY_DESCRIPTION.split(",")[0].strip(): _Text(
                        None, error=RuntimeError("detached")
                    ),
                    "main": "",
                }
            )
        )
    assert caught.value.title == "Backend разработчик"
    assert caught.value.company == "Компания"


def _ref(external_id):
    return JobRef(external_id=external_id, url=f"https://fake/{external_id}")


def _configure_apply(monkeypatch, adapter, evaluated):
    async def evaluate_impl(posting, *_args, **_kwargs):
        evaluated.append(posting.external_id)
        return evaluation("apply")

    async def letter(*_args, **_kwargs):
        return "Fixture letter"

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _adapter_id: adapter)
    monkeypatch.setattr(workflow, "evaluate", evaluate_impl)
    monkeypatch.setattr(workflow, "write_cover_letter", letter)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_result", ["", " \n\t ", "typed"])
async def test_unavailable_description_drains_pipeline_and_healthy_vacancy_continues(
    workflow_runtime, monkeypatch, bad_result
):
    sessions, session_id = workflow_runtime
    evaluated = []

    class Adapter(FakeAdapter):
        def __init__(self):
            super().__init__([_ref("bad"), _ref("healthy"), _ref("bad")])
            self.calls = {"bad": 0, "healthy": 0}
            self.submitted = []

        async def extract_job(self, page):
            external_id = self.current_ref.external_id
            self.calls[external_id] += 1
            if external_id == "bad":
                if bad_result == "typed":
                    raise JobDescriptionUnavailable(title="Bad role", company="Bad Co")
                posting = await super().extract_job(page)
                posting.description = bad_result
                return posting
            return await super().extract_job(page)

        async def submit_application(self, page):
            self.submitted.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter()
    monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
    _configure_apply(monkeypatch, adapter, evaluated)
    await workflow.WorkflowManager().run(session_id)

    assert adapter.calls == {"bad": 3, "healthy": 1}
    assert evaluated == ["healthy"]
    assert adapter.submitted == ["healthy"]
    with sessions() as db:
        session = db.get(JobSession, session_id)
        vacancies = {item.external_id: item for item in db.scalars(select(Vacancy))}
        pipeline = {item.external_id: item for item in db.scalars(select(PipelineItem))}
        events = list(db.scalars(select(BrowserEvent)))
        assert session.status == SessionStatus.COMPLETED
        assert session.counters["errors"] == 1
        assert session.counters["viewed"] == 2
        assert "description_read_attempts" not in (session.recovery or {})
        assert vacancies["bad"].state == "ERROR"
        assert vacancies["bad"].data["error_code"] == "VACANCY_DESCRIPTION_UNAVAILABLE"
        assert pipeline["bad"].status == "completed"
        assert pipeline["healthy"].status == "completed"
        assert not any(event.event_type == "recovery_exhausted" for event in events)


@pytest.mark.asyncio
async def test_transient_description_failure_succeeds_on_third_read(workflow_runtime, monkeypatch):
    sessions, session_id = workflow_runtime
    evaluated = []

    class Adapter(FakeAdapter):
        calls = 0

        def __init__(self):
            super().__init__([_ref("late")])
            self.submitted = []

        async def extract_job(self, page):
            self.calls += 1
            if self.calls < 3:
                raise JobDescriptionUnavailable(title="Late role", company="Late Co")
            return await super().extract_job(page)

        async def submit_application(self, page):
            self.submitted.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter()
    monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
    _configure_apply(monkeypatch, adapter, evaluated)
    await workflow.WorkflowManager().run(session_id)

    assert adapter.calls == 3
    assert evaluated == ["late"]
    assert adapter.submitted == ["late"]
    with sessions() as db:
        session = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy))
        assert session.status == SessionStatus.COMPLETED
        assert session.counters.get("errors", 0) == 0
        assert session.counters["submitted"] == 1
        assert "description_read_attempts" not in (session.recovery or {})
        assert vacancy.state != "ERROR"


@pytest.mark.asyncio
async def test_description_retry_budget_survives_cancelled_run(workflow_runtime, monkeypatch):
    sessions, session_id = workflow_runtime

    class Interrupted(FakeAdapter):
        async def extract_job(self, page):
            raise JobDescriptionUnavailable(title="Role")

        async def open_job(self, page, ref):
            raise asyncio.CancelledError()

    monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
    ref = _ref("durable")
    manager = workflow.WorkflowManager()
    with pytest.raises(asyncio.CancelledError):
        await manager._extract_job_with_description_retries(session_id, Interrupted([ref]), object(), ref)
    with sessions() as db:
        assert db.get(JobSession, session_id).recovery["description_read_attempts"] == {"durable": 1}

    class Resumed(FakeAdapter):
        calls = 0

        async def extract_job(self, page):
            self.calls += 1
            raise JobDescriptionUnavailable(title="Role")

    resumed = Resumed([ref])
    with pytest.raises(JobDescriptionUnavailable):
        await workflow.WorkflowManager()._extract_job_with_description_retries(
            session_id, resumed, object(), ref
        )
    assert resumed.calls == 2
    with sessions() as db:
        assert db.get(JobSession, session_id).recovery["description_read_attempts"] == {"durable": 3}


@pytest.mark.asyncio
async def test_captcha_during_description_retry_pauses_without_recording_vacancy_error(
    workflow_runtime, monkeypatch
):
    sessions, session_id = workflow_runtime

    class Adapter(FakeAdapter):
        def __init__(self, refs):
            super().__init__(refs)
            self.opens = 0

        async def open_job(self, page, ref):
            self.opens += 1
            await super().open_job(page, ref)

        async def extract_job(self, page):
            raise JobDescriptionUnavailable(title="Role")

        async def detect_blockers(self, page):
            if self.current_ref is not None and self.opens >= 2:
                return [Blocker(kind="captcha", message="captcha")]
            return []

    monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
    ref = _ref("captcha")
    adapter = Adapter([ref])
    closed = []

    async def track_close(_session_id):
        closed.append(_session_id)

    monkeypatch.setattr(workflow, "close_browser", track_close)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _adapter_id: adapter)
    await workflow.WorkflowManager().run(session_id)
    with sessions() as db:
        session = db.get(JobSession, session_id)
        assert session.status == SessionStatus.PAUSED
        assert session.counters.get("errors", 0) == 0
        assert db.scalar(select(Vacancy)) is None
        assert len(list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.event_type == "human_required"
        )))) == 1
    assert closed == []


@pytest.mark.asyncio
async def test_hh_prefetch_and_overflow_release_retry_each_bad_description_three_times(
    workflow_runtime, monkeypatch
):
    sessions, session_id = workflow_runtime
    evaluated = []
    refs = [
        JobRef(external_id=f"bad-{index}", url=f"https://hh.ru/vacancy/bad-{index}")
        for index in range(11)
    ] + [JobRef(external_id="healthy", url="https://hh.ru/vacancy/healthy")]

    class Adapter(FakeAdapter):
        def __init__(self):
            super().__init__(refs)
            self.site_id = "hh"
            self.calls = {ref.external_id: 0 for ref in refs}

        async def extract_job(self, page):
            external_id = self.current_ref.external_id
            self.calls[external_id] += 1
            posting = await super().extract_job(page)
            if external_id.startswith("bad-"):
                posting.description = " \n\t "
            return posting

    adapter = Adapter()

    class FakeHHSearch:
        def __init__(self, raw_adapter, *_args, **_kwargs):
            self.adapter = raw_adapter
            self.search_exhausted = True
            self.last_discovery_batch = {}

        def __getattr__(self, name):
            return getattr(self.adapter, name)

        async def open_search(self, page, filters):
            return await self.adapter.open_search(page, filters)

        async def collect_job_refs(self, page):
            return await self.adapter.collect_job_refs(page)

        async def collect_more_job_refs(self, page):
            return []

        def search_checkpoint(self):
            return {}

    with sessions() as db:
        session = db.get(JobSession, session_id)
        session.adapter_id = "hh"
        session.application_limit = 1
        session.started_at = datetime.now(timezone.utc)
        session.counters = {
            "viewed": 1, "filtered": 0, "matched": 0, "submitted": 0,
            "reported": 0, "already_applied": 0, "errors": 0,
        }
        db.add(
            Vacancy(
                session_id=session_id,
                source="hh",
                external_id="bad-0",
                url="https://hh.ru/vacancy/bad-0",
                title="Previously extracted role",
                state="EXTRACTED",
                data={
                    "source": "hh",
                    "external_id": "bad-0",
                    "url": "https://hh.ru/vacancy/bad-0",
                    "title": "Previously extracted role",
                    "description": " \n\t ",
                    "company": "Fixture company",
                },
            )
        )
        snapshot = _normalize_extracted(
            {
                "external_id": "fixture",
                "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"},
                "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            },
            adapter_id="hh",
            source_url="https://hh.ru/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()

    monkeypatch.setattr(workflow.WorkflowManager, "description_retry_seconds", 0)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _adapter_id: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", FakeHHSearch)

    async def no_portfolio(*_args, **_kwargs):
        return []

    monkeypatch.setattr(workflow, "plan_portfolio", no_portfolio)
    _configure_apply(monkeypatch, adapter, evaluated)
    await workflow.WorkflowManager().run(session_id)

    assert adapter.calls == {**{f"bad-{index}": 3 for index in range(11)}, "healthy": 1}
    assert evaluated == ["healthy"]
    with sessions() as db:
        session = db.get(JobSession, session_id)
        assert session.status == SessionStatus.COMPLETED
        assert session.counters["errors"] == 11
        assert session.counters["viewed"] == 12
        assert session.counters["submitted"] == 1
        assert "description_read_attempts" not in (session.recovery or {})
        pipeline = {
            item.external_id: item
            for item in db.scalars(select(PipelineItem).where(PipelineItem.session_id == session_id))
        }
        assert all(pipeline[f"bad-{index}"].status == "completed" for index in range(11))
        assert pipeline["healthy"].status == "completed"
