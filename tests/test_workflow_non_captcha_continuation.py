from __future__ import annotations

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
from backend.orchestrator import workflow
from backend.persistence.database import Base
from backend.persistence.models import CandidateProfile, JobSession, Resume, Vacancy
from backend.schemas.domain import JobEvaluation, JobPosting, SessionStatus


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

    async def fill_application(self, page, plan):
        return FillResult(success=True)

    async def submit_application(self, page):
        if self.submission_error:
            raise RuntimeError(self.submission_error)
        return SubmissionResult(status="submitted", message="submitted")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        profile = CandidateProfile(full_name="Test", contacts={}, education=[], languages=[])
        db.add(profile)
        db.flush()
        db.add(Resume(profile_id=profile.id, name="Resume", desired_title="Role",
                      selected_for_matching=True))
        item = JobSession(profile_id=profile.id, adapter_id="fake",
                          viewed_limit=None, application_limit=None,
                          status=SessionStatus.CREATED, counters={})
        db.add(item)
        db.commit()
        session_id = item.id
    monkeypatch.setattr(workflow, "SessionLocal", sessions)
    monkeypatch.setattr(workflow, "get_browser", lambda session_id: SimpleNamespace(page=object()))
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    return sessions, session_id


async def run_workflow(runtime, monkeypatch, adapter, evaluate_impl=None):
    sessions, session_id = runtime
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda adapter_id: adapter)
    if evaluate_impl is None:
        async def evaluate_impl(*args, **kwargs):
            return evaluation()
    monkeypatch.setattr(workflow, "evaluate", evaluate_impl)
    await workflow.WorkflowManager()._run(session_id)
    return sessions, session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "state"), [
    ("test", "NEEDS_REVIEW"), ("unknown_form", "UNKNOWN"),
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
        assert vacancies["test"].state == "NEEDS_REVIEW"
        assert vacancies["next"].state == "REJECTED_BY_MODEL"
        assert item.counters["filtered"] == 1
        assert item.counters["skipped_test"] == 1
        assert item.counters["review"] == 1


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
async def test_model_unavailable_marks_error_and_continues(runtime, monkeypatch):
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
        assert item.counters["errors"] == 1
        assert states == {"bad": "ERROR", "next": "REJECTED_BY_MODEL"}


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
        assert vacancy.state == "UNKNOWN"


@pytest.mark.asyncio
async def test_long_mixed_blocker_run_finishes(runtime, monkeypatch):
    refs = [JobRef(external_id=str(i), url=f"https://fake/{i}") for i in range(200)]
    kinds = ("test", "unknown_form", "mfa", "blocked", "sensitive")
    blockers = {str(i): kinds[(i // 2) % len(kinds)] for i in range(0, 200, 2)}
    sessions, session_id = await run_workflow(
        runtime, monkeypatch, FakeAdapter(refs, job_blockers=blockers)
    )
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        vacancies = list(db.scalars(select(Vacancy)))
        assert len(vacancies) == 200
        assert all(v.state != "EVALUATING" for v in vacancies)
        assert db.get(JobSession, session_id).counters["errors"] == 80


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "state"), [
    ("test", "NEEDS_REVIEW"), ("unknown_form", "UNKNOWN"), ("blocked", "ERROR"),
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
        vacancies = {v.external_id: v.state for v in db.scalars(select(Vacancy))}
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert vacancies == {"bad": state, "next": "SUBMITTED"}


@pytest.mark.asyncio
async def test_manual_review_decision_becomes_error_and_continues(runtime, monkeypatch):
    refs = [JobRef(external_id="bad", url="https://fake/bad"),
            JobRef(external_id="next", url="https://fake/next")]
    calls = 0

    async def evaluate_impl(*args, **kwargs):
        nonlocal calls
        calls += 1
        return evaluation("manual_review" if calls == 1 else "skip")

    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), evaluate_impl)
    with sessions() as db:
        states = {v.external_id: v.state for v in db.scalars(select(Vacancy))}
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert states == {"bad": "ERROR", "next": "REJECTED_BY_MODEL"}


@pytest.mark.asyncio
async def test_letter_model_unavailable_marks_error_and_continues(runtime, monkeypatch):
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
    sessions, session_id = await run_workflow(runtime, monkeypatch, FakeAdapter(refs), apply_all)
    with sessions() as db:
        states = {v.external_id: v.state for v in db.scalars(select(Vacancy))}
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert states == {"bad": "ERROR", "next": "SUBMITTED"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_kwargs", "state"), [
    ({"questions": ["Неизвестный вопрос"]}, "UNKNOWN"),
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
