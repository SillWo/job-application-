from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import (
    ApplicationForm,
    Blocker,
    FillResult,
    JobRef,
    LoginState,
    SubmissionResult,
)
from backend.api.router import pause_session
from backend.intelligence.broker_gateway import BrokeredModelGateway
from backend.intelligence.letter_writer import CoverLetterValidationError
from backend.intelligence.model_broker import ModelRequestClient
from backend.orchestrator import hh_application, workflow
from backend.persistence.database import Base
from backend.persistence.models import (
    Application,
    BrowserEvent,
    CoverLetter,
    JobSession,
    Notification,
    Vacancy,
)
from backend.persistence.pipeline_models import PipelineItem
from backend.schemas.domain import (
    ApplicationField,
    ApplicationPlan,
    FormAnswer,
    JobEvaluation,
    JobPosting,
    SessionStatus,
)
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot


def evaluation(decision: str = "skip") -> JobEvaluation:
    return JobEvaluation(decision=decision, score=80 if decision == "apply" else 20,
                         confidence=0.9, category="test", reason="fixture")


class FakeAdapter:
    site_id = "fake"
    search_exhausted = True

    def __init__(
        self,
        refs,
        *,
        search_blocker=None,
        job_blockers=None,
        submit_blockers=None,
        application_error=None,
        submission_error=None,
        questions=None,
    ):
        self.refs = refs
        self.search_blocker = search_blocker
        self.job_blockers = job_blockers or {}
        self.submit_blockers = submit_blockers or {}
        self.application_error = application_error
        self.submission_error = submission_error
        self.questions = questions or []
        self.current_ref = None
        self.detect_count = {}
        self.search_checked = False

    async def get_login_state(self, page):
        return LoginState(authenticated=True, message="ok")

    async def open_search(self, page, filters):
        self.current_ref = None
        return None

    async def collect_job_refs(self, page):
        return self.refs

    async def collect_more_job_refs(self, page):
        return []

    async def open_job(self, page, ref):
        self.current_ref = ref

    async def detect_blockers(self, page):
        if self.current_ref is None:
            if self.search_checked:
                return []
            self.search_checked = True
            kind = self.search_blocker
        else:
            ref_id = self.current_ref.external_id
            count = self.detect_count.get(ref_id, 0)
            self.detect_count[ref_id] = count + 1
            kind = self.job_blockers.get(ref_id) if count == 0 else self.submit_blockers.get(ref_id)
        return [Blocker(kind=kind, message=f"{kind} blocker")] if kind else []

    async def extract_job(self, page):
        return JobPosting(source=self.site_id, external_id=self.current_ref.external_id,
                          url=self.current_ref.url, title=f"Vacancy {self.current_ref.external_id}",
                          description="Description")

    async def open_application(self, page):
        if self.application_error:
            raise RuntimeError(self.application_error)
        return ApplicationForm(questions=self.questions)

    async def prepare_application(self, page, plan):
        return ApplicationForm()

    async def read_application(self, page):
        return ApplicationForm()

    async def fill_application(self, page, plan):
        return FillResult(success=True)

    async def submit_application(self, page):
        if self.submission_error:
            raise RuntimeError(self.submission_error)
        return SubmissionResult(status="submitted", message="submitted")


    async def verify_submission(self, page):
        return SubmissionResult(status="unknown", message="No confirmation")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        item = JobSession(adapter_id="fake",
                          application_limit=None,
                          status=SessionStatus.CREATED, counters={})
        db.add(item)
        db.flush()
        snapshot = _normalize_extracted(
            {"external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
             "target": {"title": "Role"}, "about": "Fixture professional background",
             "skills": [{"name": "Python"}]},
            adapter_id="fake", source_url="https://fake/resume/fixture",
        )
        persist_session_snapshot(db, item.id, snapshot)
        db.commit()
        session_id = item.id
    monkeypatch.setattr(workflow.WorkflowManager, "retry_base_seconds", 0)
    monkeypatch.setattr(workflow, "SessionLocal", sessions)
    monkeypatch.setattr(workflow, "get_browser", lambda session_id: SimpleNamespace(page=object()))
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    return sessions, session_id


async def run_workflow(runtime, monkeypatch, adapter, evaluate_impl=None, *, timeout=20):
    sessions, session_id = runtime
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda adapter_id: adapter)
    if evaluate_impl is None:
        async def evaluate_impl(*args, **kwargs):
            return evaluation()
    monkeypatch.setattr(workflow, "evaluate", evaluate_impl)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=timeout)
    return sessions, session_id


def test_production_workflow_gateway_uses_durable_broker(runtime, monkeypatch):
    _sessions, session_id = runtime
    direct_gateway = workflow._INJECTABLE_DIRECT_GATEWAY
    monkeypatch.setattr(workflow, "ModelGateway", direct_gateway)

    def fail_if_provider_gateway_is_constructed(*args, **kwargs):
        raise AssertionError("workflow constructed a provider-bearing ModelGateway")

    monkeypatch.setattr(direct_gateway, "__init__", fail_if_provider_gateway_is_constructed)
    gateway = workflow.WorkflowManager()._model_gateway(session_id, "fake")

    assert isinstance(gateway, BrokeredModelGateway)
    assert isinstance(gateway.client, ModelRequestClient)
    assert gateway.session_id == session_id
    assert gateway.site_id == "fake"


def test_cancelled_session_terminalizes_pending_vacancy_without_error(runtime):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.status = SessionStatus.CANCELLED
        item.counters = {"errors": 2}
        vacancy = Vacancy(
            session_id=session_id,
            source="fake",
            external_id="pending",
            url="https://fake/pending",
            title="Pending",
            state="EVALUATING",
            data={},
        )
        db.add(vacancy)
        db.flush()

        terminalized = workflow.WorkflowManager()._terminalize_pending_vacancies(db, item)

        assert terminalized == 1
        assert vacancy.state == "CANCELLED"
        assert vacancy.data["cancellation_code"] == "SESSION_CANCELLED"
        assert item.counters == {"errors": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("submission_after_failure", [False, True], ids=["retry-at-window-end", "retry-after-submission"])
async def test_historical_hh_duplicates_release_queue_for_new_reference(
    runtime, monkeypatch, submission_after_failure
):
    sessions, session_id = runtime
    current_ids = ["retry", "new"] if submission_after_failure else ["new"]
    refs = [
        JobRef(external_id=f"historic-{index}", url=f"https://hh.ru/vacancy/historic-{index}")
        for index in range(10)
    ] + [JobRef(external_id=ref_id, url=f"https://hh.ru/vacancy/{ref_id}") for ref_id in current_ids]
    adapter = FakeAdapter(refs)
    adapter.site_id = "hh"
    opened = []
    original_open_job = adapter.open_job

    async def tracked_open_job(page, ref):
        opened.append(ref.external_id)
        await original_open_job(page, ref)

    adapter.open_job = tracked_open_job
    original_extract_job = adapter.extract_job
    extraction_attempts = 0

    async def fail_extraction_once(page):
        nonlocal extraction_attempts
        extraction_attempts += 1
        if extraction_attempts == 1:
            raise RuntimeError("temporary extraction outage")
        return await original_extract_job(page)

    adapter.extract_job = fail_extraction_once
    discovery_refills = 0
    with sessions() as db:
        current = db.get(JobSession, session_id)
        current.adapter_id = "hh"
        current.application_limit = len(current_ids)
        snapshot = _normalize_extracted(
            {"external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
             "target": {"title": "Role"}, "about": "Fixture professional background",
             "skills": [{"name": "Python"}]},
            adapter_id="hh", source_url="https://hh.ru/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()

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
            nonlocal discovery_refills
            discovery_refills += 1
            return []

        def search_checkpoint(self):
            return {}

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _adapter_id: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", FakeHHSearch)

    async def no_portfolio(*_args, **_kwargs):
        return []

    monkeypatch.setattr(workflow, "plan_portfolio", no_portfolio)

    async def apply_new_reference(*_args, **_kwargs):
        return evaluation("apply")

    monkeypatch.setattr(workflow, "evaluate", apply_new_reference)

    async def short_cover_letter(*_args, **_kwargs):
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", short_cover_letter)
    recoveries = []
    original_recover = workflow.WorkflowManager._recover

    async def capture_recovery(self, current_session_id, exc):
        recoveries.append(repr(exc))
        return await original_recover(self, current_session_id, exc)

    monkeypatch.setattr(workflow.WorkflowManager, "_recover", capture_recovery)
    with sessions() as db:
        historical_session = JobSession(
            adapter_id="hh", status=SessionStatus.COMPLETED, counters={"submitted": 99}
        )
        db.add(historical_session)
        db.flush()
        db.add_all([
            Vacancy(
                session_id=historical_session.id,
                source="hh",
                external_id=f"historic-{index}",
                url=f"https://hh.ru/vacancy/historic-{index}",
                title=f"Historical role {index}",
                state="SUBMITTED",
                data={"preserve": index},
            )
            for index in range(10)
        ])
        db.flush()
        db.add_all([
            Application(vacancy_id=vacancy.id, status="submitted")
            for vacancy in db.scalars(select(Vacancy).where(
                Vacancy.session_id == historical_session.id,
            ))
        ])
        db.commit()

    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=5)

    assert opened == (["retry", "new", "retry"] if submission_after_failure else ["new", "new"])
    assert extraction_attempts == len(current_ids) + 1
    assert discovery_refills == 0
    assert any("RecoverableFailure" in error for error in recoveries)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        assert item.status == SessionStatus.COMPLETED, (item.status, recoveries, item.recovery)
        assert item.counters["submitted"] == len(current_ids)
        assert item.counters["errors"] == 0
        vacancies = list(db.scalars(select(Vacancy).where(
            Vacancy.session_id == historical_session.id
        )))
        assert [vacancy.data for vacancy in vacancies] == [
            {"preserve": index} for index in range(10)
        ]
        pipeline_items = list(db.scalars(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.site_id == "hh",
        )))
        by_id = {row.external_id: row for row in pipeline_items}
        assert all(by_id[f"historic-{index}"].status == "completed" for index in range(10))
        for ref_id in current_ids:
            assert by_id[ref_id].status == "completed"
            assert db.scalar(select(Vacancy).where(
                Vacancy.session_id == session_id,
                Vacancy.external_id == ref_id,
            )).state == "SUBMITTED"


def test_internal_form_transition_is_recorded_as_an_error(runtime):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = Vacancy(session_id=session_id, source="hh", external_id="form", url="u", title="T", state="SUBMITTING", data={})
        db.add(vacancy); db.commit()
        workflow.WorkflowManager()._record_submission(
            db, item, vacancy, SubmissionResult(status="needs_input", message="HH.ru ожидает ответа"),
        )
        db.commit()
        assert vacancy.state == "ERROR"
        assert vacancy.data["error_code"] == "APPLICATION_FORM_UNRESOLVED"
        assert item.counters["errors"] == 1


@pytest.mark.asyncio
async def test_high_viewed_count_does_not_stop_session_but_application_limit_does(runtime, monkeypatch):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.counters = {"viewed": 1000}
        item.started_at = datetime.now(timezone.utc)
        db.commit()
    refs = [JobRef(external_id="one", url="https://fake/one"), JobRef(external_id="two", url="https://fake/two")]
    async def skip_evaluation(*args, **kwargs):
        return evaluation("skip")
    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), evaluate_impl=skip_evaluation)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["viewed"] == 1002
        assert item.stop_reason != "Достигнут лимит просмотра вакансий"

    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.status = SessionStatus.CREATED
        item.finished_at = None
        item.stop_reason = None
        item.application_limit = 1
        item.counters = {"viewed": 1000, "submitted": 1}
        db.commit()
    async def apply_evaluation(*args, **kwargs):
        return evaluation("apply")
    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), evaluate_impl=apply_evaluation)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        assert item.status == SessionStatus.COMPLETED
        assert item.stop_reason == "Достигнут лимит отправленных откликов"


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "state"), [
    ("test", "REJECTED_BY_MODEL"), ("unknown_form", "ERROR"),
    ("mfa", "ERROR"),
    ("blocked", "ERROR"),
    ("sensitive", "ERROR"),
])
async def test_open_job_non_captcha_blocker_is_saved_and_next_ref_runs(
    runtime, monkeypatch, kind, state
):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, job_blockers={"bad": kind})
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancies = {v.external_id: v for v in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert vacancies["bad"].state == state
        assert vacancies["next"].state == "REJECTED_BY_MODEL"


@pytest.mark.asyncio
async def test_test_assignment_is_filtered_and_next_ref_runs(runtime, monkeypatch):
    refs = [JobRef(external_id="test", url="https://fake/test"),
            JobRef(external_id="next", url="https://fake/next")]
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, job_blockers={"test": "test"})
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancies = {v.external_id: v for v in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert vacancies["test"].state == "REJECTED_BY_MODEL"
        assert vacancies["next"].state == "REJECTED_BY_MODEL"
        assert item.counters["filtered"] == 2


@pytest.mark.asyncio
async def test_search_non_captcha_blocker_continues(runtime, monkeypatch):
    refs = [JobRef(external_id="next", url="https://fake/next")]
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, search_blocker="mfa")
    )
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert db.scalar(select(Vacancy)).state == "REJECTED_BY_MODEL"


@pytest.mark.asyncio
async def test_captcha_still_pauses(runtime, monkeypatch):
    refs = [JobRef(external_id="never", url="https://fake/never")]
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, search_blocker="captcha")
    )
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.PAUSED
        assert db.scalar(select(Vacancy)) is None


@pytest.mark.asyncio
async def test_model_unavailable_recovers_without_error_and_continues(runtime, monkeypatch):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]
    calls = 0

    async def evaluate_impl(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelUnavailable("offline")
        return evaluation()

    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), evaluate_impl)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        states = {v.external_id: v.state for v in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert item.counters.get("errors", 0) == 0
        assert item.counters["viewed"] == 2
        assert states == {"bad": "REJECTED_BY_MODEL", "next": "REJECTED_BY_MODEL"}
        assert calls == 3


@pytest.mark.asyncio
async def test_application_attempt_failure_increments_error_once(runtime, monkeypatch):
    refs = [JobRef(external_id="apply", url="https://fake/apply")]

    async def evaluate_impl(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = await run_workflow(
        runtime,
        monkeypatch,
        FakeAdapter(refs, application_error="form unavailable"),
        evaluate_impl,
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy))
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["errors"] == 1
        assert vacancy.state == "ERROR"
        assert vacancy.data["error_code"] == "VACANCY_PROCESSING_FAILED"


@pytest.mark.asyncio
async def test_apply_branch_uses_cover_letter_contract_without_extra_kwargs(runtime, monkeypatch):
    refs = [JobRef(external_id="apply", url="https://fake/apply")]
    calls = []

    async def apply_evaluation(*args, **kwargs):
        return evaluation("apply")

    async def strict_writer(
        job, profile, resumes, gateway, preference_policy=None, *,
        cover_letter_auto=True, cover_letter_template="", private_view=None,
    ):
        calls.append((job.external_id, profile.get("gender"), cover_letter_auto, cover_letter_template))
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", strict_writer)
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs), apply_evaluation,
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy))
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["matched"] == 1
        assert item.counters["submitted"] == 1
        assert vacancy.state == "SUBMITTED"
    assert calls == [("apply", "male", True, "")]


@pytest.mark.asyncio
async def test_snapshot_application_answers_do_not_use_session_memory(monkeypatch):
    field = ApplicationField(id="city", label="Город")
    form = ApplicationForm(fields=[field])
    filled = []

    class FormAdapter:
        async def prepare_application(self, page, plan):
            return form

        async def read_application(self, page):
            return form

        async def fill_application(self, page, plan):
            filled.append(plan.form_answers["city"].values)
            return FillResult(success=True, answered_fields=["city"])

        async def submit_application(self, page):
            return SubmissionResult(status="submitted", message="submitted")

    async def prepare_answers(*args, memory=None, **kwargs):
        assert memory is None
        plan = args[2]
        plan.form_answers = {
            "city": FormAnswer(field=field, values=["Красноярск"], source="snapshot")
        }
        plan.unanswered_fields = {}
        return plan

    monkeypatch.setattr(hh_application, "prepare_answers", prepare_answers)
    outcome = await hh_application.complete_application(
        FormAdapter(),
        object(),
        ApplicationPlan(vacancy_id=1, resume_file="", submission_allowed=True),
        JobPosting(source="fake", external_id="one", url="https://fake/one",
                   title="Role", description="Description"),
        {},
        [{"about": "Fixture professional background"}],
        "",
        object(),
        lambda plan: True,
    )

    assert outcome.error_code is None
    assert outcome.submission is not None
    assert outcome.submission.status == "submitted"
    assert filled == [["Красноярск"]]


@pytest.mark.asyncio
async def test_questionnaire_model_timeout_recovers_and_submits_once(runtime, monkeypatch):
    field = ApplicationField(id="city", label="Город")
    form = ApplicationForm(fields=[field])

    class QuestionnaireAdapter(FakeAdapter):
        site_id = "hh"
        search_exhausted = False

        def __init__(self):
            super().__init__([JobRef(external_id="questionnaire", url="https://fake/questionnaire")])
            self.open_application_calls = 0
            self.fill_calls = 0
            self.submit_calls = 0

        async def open_application(self, page):
            self.open_application_calls += 1
            return form

        async def prepare_application(self, page, plan):
            return form

        async def read_application(self, page):
            return form

        async def fill_application(self, page, plan):
            self.fill_calls += 1
            assert plan.form_answers["city"].values == ["Красноярск"]
            return FillResult(success=True, answered_fields=["city"])

        async def can_retry_application(self, page):
            return True

        async def verify_submission(self, page):
            return SubmissionResult(status="unknown", message="No send was confirmed")

        async def submit_application(self, page):
            self.submit_calls += 1
            return SubmissionResult(status="submitted", message="submitted")

    adapter = QuestionnaireAdapter()
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.application_limit = 1
        item.recovery = {"search_filters": {}}
        snapshot = _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            },
            adapter_id="hh", source_url="https://fake/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    async def apply_all(*_args, **_kwargs):
        return evaluation("apply")

    monkeypatch.setattr(workflow, "evaluate", apply_all)
    async def write_letter(*_args, **_kwargs):
        return "Synthetic cover letter for questionnaire test"

    monkeypatch.setattr(workflow, "write_cover_letter", write_letter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    prepare_calls = 0

    async def timeout_then_answer(_gateway, _form, plan, *_args, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            raise workflow.ModelTimeout("questionnaire model timeout")
        plan.form_answers = {
            "city": FormAnswer(field=field, values=["Красноярск"], source="snapshot")
        }
        plan.unanswered_fields = {}
        return plan

    monkeypatch.setattr(hh_application, "prepare_answers", timeout_then_answer)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy).where(Vacancy.session_id == session_id))
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "recovery_retry",
        )))
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["submitted"] == 1
        assert item.counters["errors"] == 0
        assert vacancy.state == "SUBMITTED"
        assert len(retries) == 1
        assert retries[0].data["error_type"] == "ModelTimeout"
    assert prepare_calls == 2
    assert adapter.open_application_calls == 2
    assert adapter.fill_calls == 1
    assert adapter.submit_calls == 1


@pytest.mark.asyncio
async def test_long_mixed_blocker_run_finishes(runtime, monkeypatch):
    refs = [JobRef(external_id=str(i), url=f"https://fake/{i}") for i in range(200)]
    kinds = ("test", "unknown_form", "mfa", "blocked", "sensitive")
    blockers = {str(i): kinds[(i // 2) % len(kinds)] for i in range(0, 200, 2)}
    # This stress case intentionally exercises 200 durable queue transitions.
    # SQLite's synchronous commits dominate the measured runtime (~13s across
    # 1,276 commits), so it gets a wider wall clock budget than small fixtures.
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, job_blockers=blockers), timeout=40
    )
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        vacancies = list(db.scalars(select(Vacancy)))
        assert len(vacancies) == 200
        assert all(v.state != "EVALUATING" for v in vacancies)
        assert db.get(JobSession, session_id).counters["errors"] == 80


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "state"), [
    ("test", "REJECTED_BY_MODEL"), ("unknown_form", "ERROR"), ("blocked", "ERROR"),
])
async def test_pre_submit_non_captcha_blocker_continues(runtime, monkeypatch, kind, state):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, submit_blockers={"bad": kind}), apply_all
    )
    with sessions() as db:
        rows = {v.external_id: v for v in db.scalars(select(Vacancy))}
        vacancies = {external_id: vacancy.state for external_id, vacancy in rows.items()}
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert vacancies == {"bad": state, "next": "SUBMITTED"}
        notifications = list(db.scalars(select(Notification).where(Notification.source_type == "vacancy")))
        assert len(notifications) == (0 if state == "REJECTED_BY_MODEL" else 1)
        if notifications:
            assert notifications[0].source_id == str(rows["bad"].id)
            assert notifications[0].kind == f"vacancy_{state.lower()}"
            assert notifications[0].target_path == "/vacancies"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_letter_model_unavailable_recovers_without_duplicate_match_count(runtime, monkeypatch):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]
    letter_calls = 0

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        nonlocal letter_calls
        letter_calls += 1
        if letter_calls == 1:
            raise workflow.ModelUnavailable("offline")
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = runtime
    with sessions() as db:
        db.get(JobSession, session_id).guaranteed_application = True
        db.commit()
    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), apply_all)
    with sessions() as db:
        states = {v.external_id: v.state for v in db.scalars(select(Vacancy))}
        item = db.get(JobSession, session_id)
        assert item.status == SessionStatus.COMPLETED
        assert item.counters.get("errors", 0) == 0
        assert item.counters["matched"] == 2
        assert item.counters["submitted"] == 2
        assert states == {"bad": "SUBMITTED", "next": "SUBMITTED"}


@pytest.mark.asyncio
async def test_invalid_cover_letter_is_retried_before_submission(runtime, monkeypatch):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]
    letter_calls = 0

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        nonlocal letter_calls
        letter_calls += 1
        if letter_calls == 1:
            raise CoverLetterValidationError("не выполнено особое условие")
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), apply_all)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        rows = {v.external_id: v for v in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert letter_calls == 3
        assert item.counters["matched"] == 2
        assert item.counters["submitted"] == 2
        assert rows["bad"].state == "SUBMITTED"
        assert "cover_letter_attempts" not in rows["bad"].data
        assert "cover_letter_error" not in rows["bad"].data
        assert rows["next"].state == "SUBMITTED"
        assert db.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == rows["bad"].id)) is not None
        events = list(db.scalars(select(workflow.BrowserEvent).where(
            workflow.BrowserEvent.session_id == session_id,
            workflow.BrowserEvent.event_type == "vacancy_retry",
        )))
        assert any(event.data.get("kind") == "cover_letter" for event in events)


@pytest.mark.asyncio
async def test_cached_cover_letter_over_custom_limit_is_regenerated_and_updated(runtime, monkeypatch):
    sessions, session_id = runtime
    refs = [JobRef(external_id="cached", url="https://fake/cached")]
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.cover_letter_max_words = 3
        vacancy = Vacancy(
            session_id=session_id,
            source="fake",
            external_id="cached",
            url="https://fake/cached",
            title="Vacancy cached",
            # Processing states are resumed by the workflow; an EXTRACTED row
            # is intentionally skipped during a fresh search pass.
            state="EVALUATING",
            data={},
        )
        db.add(vacancy)
        db.flush()
        db.add(CoverLetter(vacancy_id=vacancy.id, text="одно два три четыре"))
        db.commit()

    calls = []

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def regenerated(*args, **kwargs):
        calls.append(kwargs.get("cover_letter_max_words"))
        return "одно два"

    monkeypatch.setattr(workflow, "write_cover_letter", regenerated)
    await run_workflow(runtime, monkeypatch, FakeAdapter(refs), apply_all)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy).where(Vacancy.external_id == "cached"))
        assert item.status == SessionStatus.COMPLETED
        assert calls == [3]
        assert db.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id)).text == "одно два"
        assert vacancy.state == "SUBMITTED"


@pytest.mark.asyncio
async def test_invalid_cover_letter_is_filtered_after_bounded_retries(runtime, monkeypatch):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]
    letter_calls = 0

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        nonlocal letter_calls
        letter_calls += 1
        if args[0].external_id == "bad":
            raise CoverLetterValidationError("не выполнено особое условие")
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), apply_all)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        rows = {v.external_id: v for v in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert letter_calls == workflow._COVER_LETTER_RETRY_LIMIT + 1
        assert item.counters["submitted"] == 1
        assert rows["bad"].state == "ERROR"
        assert rows["bad"].data["error_code"] == "VACANCY_PROCESSING_FAILED"
        assert "не выполнено особое условие" in rows["bad"].data["cover_letter_error"]
        assert rows["bad"].data["cover_letter_attempts"] == workflow._COVER_LETTER_RETRY_LIMIT
        assert rows["next"].state == "SUBMITTED"
        assert db.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == rows["bad"].id)) is None
        events = list(db.scalars(select(workflow.BrowserEvent).where(
            workflow.BrowserEvent.session_id == session_id,
            workflow.BrowserEvent.event_type == "human_required",
        )))
        assert not any(event.data.get("kind") == "cover_letter" for event in events)



@pytest.mark.asyncio
async def test_uncaught_model_unavailable_is_retried_until_success(runtime, monkeypatch):
    manager = workflow.WorkflowManager()
    calls = 0

    async def fail_before_work(session_id):
        nonlocal calls
        calls += 1
        if calls < 5:
            raise workflow.ModelUnavailable("API unavailable")
        manager.finalize(session_id, "Доступная выдача обработана")

    monkeypatch.setattr(manager, "_run", fail_before_work)
    await asyncio.wait_for(manager.run(runtime[1]), timeout=2)
    assert calls == 5
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).status == SessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_model_unavailable_resume_retries_same_vacancy(runtime, monkeypatch):
    refs = [JobRef(external_id="first", url="https://fake/first"),
            JobRef(external_id="second", url="https://fake/second")]
    adapter = FakeAdapter(refs)
    calls = 0

    async def unavailable_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelUnavailable("temporary API outage")
        return evaluation()

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda adapter_id: adapter)
    monkeypatch.setattr(workflow, "evaluate", unavailable_once)
    manager = workflow.WorkflowManager()
    await asyncio.wait_for(manager.run(runtime[1]), timeout=5)
    assert calls == 3
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        states = {v.external_id: v.state for v in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert item.counters.get("errors", 0) == 0
        assert states == {"first": "REJECTED_BY_MODEL", "second": "REJECTED_BY_MODEL"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_kwargs", "state"), [
    ({"questions": ["Неизвестный вопрос"]}, "ERROR"),
    # A bounded submit reconciliation failure is a terminal vacancy error.
    ({"submission_error": "submit failed"}, "ERROR"),
])
async def test_form_and_submission_failures_do_not_pause(runtime, monkeypatch, adapter_kwargs, state):
    refs = [JobRef(external_id="one", url="https://fake/one"),
            JobRef(external_id="two", url="https://fake/two")]

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, **adapter_kwargs), apply_all
    )
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert {v.state for v in db.scalars(select(Vacancy))} == {state}
        item = db.get(JobSession, session_id)
        if adapter_kwargs.get("submission_error"):
            assert item.counters.get("errors") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["job", "submit"])
async def test_captcha_pauses_at_job_and_pre_submit(runtime, monkeypatch, phase):
    refs = [JobRef(external_id="captcha", url="https://fake/captcha")]
    kwargs = ({"job_blockers": {"captcha": "captcha"}} if phase == "job"
              else {"submit_blockers": {"captcha": "captcha"}})

    async def apply_all(*args, **kwargs):
        return evaluation("apply")

    async def write_cover_letter(*args, **kwargs):
        return "Сопроводительное письмо для тестовой вакансии"

    monkeypatch.setattr(workflow, "write_cover_letter", write_cover_letter)
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, **kwargs), apply_all
    )
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.PAUSED


def test_manual_pause_endpoint_is_disabled():
    with pytest.raises(HTTPException) as exc_info:
        pause_session(1, None)
    assert exc_info.value.status_code == 409
