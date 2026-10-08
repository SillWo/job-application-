"""Zarplata CV/letter recovery through the durable workflow state machine."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_workflow_non_captcha_continuation import FakeAdapter, evaluation

from backend.adapters.base.protocol import ApplicationForm, JobRef, SubmissionResult
from backend.orchestrator import workflow
from backend.persistence.models import (
    Application,
    ApplicationPlanRecord,
    JobSession,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.schemas.domain import ApplicationPlan, SessionStatus
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot

pytest_plugins = "test_workflow_non_captcha_continuation"


def configure(monkeypatch, adapter):
    async def evaluate_apply(*_args, **_kwargs):
        return evaluation("apply")

    async def write_letter(*_args, **_kwargs):
        return "Supported synthetic candidate letter"

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    monkeypatch.setattr(workflow, "evaluate", evaluate_apply)
    monkeypatch.setattr(workflow, "write_cover_letter", write_letter)


def seed_zarplata_session(sessions, session_id, *, with_vacancy=False, state="SUBMITTING",
                          progress=None, plan_mode="valid"):
    external_id = "z-letter-recovery"
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "zarplata"
        item.status = SessionStatus.RUNNING
        if state == "PARTIAL":
            item.started_at = datetime.now(timezone.utc)
        item.application_limit = 1
        item.counters = {
            "viewed": 1, "matched": 1, "submitted": 0,
            "partial": 1 if state == "PARTIAL" else 0, "errors": 0,
        }
        item.recovery = {"search_filters": {"portfolio_queries": []}}
        persist_session_snapshot(db, session_id, _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Synthetic professional background",
                "skills": [{"name": "Python"}],
            }, adapter_id="zarplata", source_url="https://zarplata.ru/resume/fixture",
        ))
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy_id = None
        if with_vacancy:
            data = {
                "source": "zarplata", "external_id": external_id,
                "url": f"https://zarplata.ru/vacancy/{external_id}",
                "title": "Synthetic Z vacancy", "description": "Synthetic posting description",
                "responsibilities": [], "required_skills": [], "optional_skills": [],
                "submission_was_absent": True,
            }
            if progress is not None:
                data["submission_progress"] = dict(progress)
            if state == "PARTIAL":
                data.update({"partial_counted": True, "partial_letter_attempts": 0})
            vacancy = Vacancy(
                session_id=session_id, source="zarplata", external_id=external_id,
                url=data["url"], title=data["title"], state=state, data=data,
            )
            db.add(vacancy)
            db.flush()
            vacancy_id = vacancy.id
            if plan_mode != "missing":
                plan = ApplicationPlan(
                    vacancy_id=vacancy.id, resume_file="fixture.pdf",
                    cover_letter="Supported synthetic candidate letter",
                ).model_dump()
                plan[workflow._RESUME_HASH_KEY] = (
                    "wrong-resume-hash" if plan_mode == "mismatch" else snapshot.content_hash
                )
                db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.commit()
    return external_id, vacancy_id


def partial_progress():
    return {
        "cv_confirmed": True,
        "cover_letter_pending": True,
        "cover_letter_confirmed": False,
    }


@pytest.mark.asyncio
async def test_zarplata_oneclick_cv_then_letter_error_persists_partial_once(runtime, monkeypatch):
    sessions, session_id = runtime
    seed_zarplata_session(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([JobRef(external_id="z-first", url="https://zarplata.ru/vacancy/z-first")])
            self.progress = None
            self.resume_calls = 0
            self.cv_upload_calls = 0

        async def can_retry_application(self, page):
            return True

        async def open_application(self, page):
            self.progress = partial_progress()
            return ApplicationForm()

        def get_submission_progress(self):
            return self.progress

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    configure(monkeypatch, adapter)
    calls = {"complete": 0}

    async def fail_letter(*_args, **_kwargs):
        calls["complete"] += 1
        return SimpleNamespace(
            submission=None, error_code="APPLICATION_FORM_UNRESOLVED",
            error_message="Letter field is unavailable", stopped=False,
            unanswered_questions=["Cover letter"],
        )

    monkeypatch.setattr(workflow, "complete_application", fail_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.scalar(select(Vacancy).where(
            Vacancy.session_id == session_id, Vacancy.external_id == "z-first",
        ))
        item = db.get(JobSession, session_id)
        assert calls["complete"] == 1, vacancy.data
        assert vacancy.state == "PARTIAL", vacancy.data
        assert vacancy.data["submission_progress"] == partial_progress()
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy.id)) is None
    assert calls["complete"] == 1
    assert adapter.resume_calls == adapter.cv_upload_calls == 0


@pytest.mark.asyncio
async def test_zarplata_unknown_letter_result_is_sent_once_then_blocked(runtime, monkeypatch):
    sessions, session_id = runtime
    seed_zarplata_session(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([JobRef(
                external_id="z-unknown-letter", url="https://zarplata.ru/vacancy/z-unknown-letter",
            )])
            self.progress = None
            self.open_calls = 0
            self.resume_calls = 0
            self.reconcile_calls = 0

        async def can_retry_application(self, page):
            return True

        async def open_application(self, page):
            self.open_calls += 1
            self.progress = partial_progress()
            return ApplicationForm()

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            self.reconcile_calls += 1
            return {**partial_progress(), "letter_recovery_safe": True}

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return self.progress

    adapter = Adapter()
    configure(monkeypatch, adapter)
    sends = 0

    async def ambiguous_letter(*_args, **kwargs):
        nonlocal sends
        sends += 1
        adapter.progress = {**partial_progress(), "cover_letter_attempted": True}
        kwargs["progress_checkpoint"](adapter.progress)
        return SimpleNamespace(
            submission=SubmissionResult(status="unknown", message="Letter result is unknown"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "complete_application", ambiguous_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.scalar(select(Vacancy).where(
            Vacancy.session_id == session_id, Vacancy.external_id == "z-unknown-letter",
        ))
        item = db.get(JobSession, session_id)
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["partial_recovery_blocked"] is True
        assert vacancy.data["submission_progress"]["cover_letter_attempted"] is True
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy.id)) is None
    assert sends == 1
    assert adapter.open_calls == 1
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_zarplata_persisted_partial_restart_resumes_only_letter(runtime, monkeypatch):
    sessions, session_id = runtime
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL", progress=partial_progress(),
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.progress = partial_progress()
            self.cv_verifications = 0
            self.cv_upload_calls = 0
            self.resume_calls = 0

        async def verify_cv_submission(self, page):
            self.cv_verifications += 1
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            assert cv_confirmed and cover_letter_pending
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return self.progress

    adapter = Adapter()
    configure(monkeypatch, adapter)

    async def accept_cached_letter(*_args, **_kwargs):
        return None

    async def finish_letter(*_args, **_kwargs):
        adapter.progress = {
            "cv_confirmed": True, "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="Letter confirmed"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", accept_cached_letter)
    monkeypatch.setattr(workflow, "complete_application", finish_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["submission_progress"]["cover_letter_confirmed"] is True
        assert item.counters["submitted"] == 1
        assert item.counters["partial"] == 0
        assert len(list(db.scalars(select(Application).where(
            Application.vacancy_id == vacancy_id,
        )))) == 1
    assert adapter.cv_verifications == 1
    assert adapter.resume_calls == 1
    assert adapter.cv_upload_calls == 0


@pytest.mark.asyncio
async def test_zarplata_submitting_without_progress_reconciles_before_letter_resume(runtime, monkeypatch):
    sessions, session_id = runtime
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="SUBMITTING",
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0
            self.resume_calls = 0
            self.cv_upload_calls = 0
            self.progress = None

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="One-click CV accepted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            assert letter_expected is True
            self.reconcile_calls += 1
            return {
                **partial_progress(), "letter_recovery_safe": True,
            }

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV verified")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            assert cv_confirmed and cover_letter_pending
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return self.progress

    adapter = Adapter()
    configure(monkeypatch, adapter)

    async def accept_cached_letter(*_args, **_kwargs):
        return None

    async def finish_letter(*_args, **_kwargs):
        adapter.progress = {
            "cv_confirmed": True, "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="Letter confirmed"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", accept_cached_letter)
    monkeypatch.setattr(workflow, "complete_application", finish_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.get(Vacancy, vacancy_id)
        item = db.get(JobSession, session_id)
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["submission_progress"]["cv_confirmed"] is True
        assert vacancy.data["submission_progress"]["cover_letter_confirmed"] is True
        assert item.counters["submitted"] == 1
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is not None
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == 1
    assert adapter.cv_upload_calls == 0


@pytest.mark.asyncio
async def test_zarplata_unsafe_letter_reconciliation_blocks_after_cv_confirmation(runtime, monkeypatch):
    sessions, session_id = runtime
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="SUBMITTING",
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0
            self.resume_calls = 0
            self.cv_upload_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="One-click CV accepted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            assert letter_expected is True
            self.reconcile_calls += 1
            return {**partial_progress(), "letter_recovery_safe": False}

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["partial_recovery_blocked"] is True
        assert vacancy.data["submission_progress"] == partial_progress()
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == adapter.cv_upload_calls == 0


@pytest.mark.asyncio
async def test_zarplata_persisted_letter_attempt_blocks_retry_even_when_reconcile_is_safe(
    runtime, monkeypatch,
):
    sessions, session_id = runtime
    attempted_progress = {**partial_progress(), "cover_letter_attempted": True}
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL",
        progress=attempted_progress,
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0
            self.resume_calls = 0

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            assert letter_expected is True
            self.reconcile_calls += 1
            return {**partial_progress(), "letter_recovery_safe": True}

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            # A fresh adapter instance does not know the durable prior intent.
            return partial_progress()

    adapter = Adapter()
    configure(monkeypatch, adapter)

    async def fail_if_letter_is_revalidated(*_args, **_kwargs):
        raise AssertionError("attempted letters must be reconciled before model validation")

    async def fail_if_complete_is_repeated(*_args, **_kwargs):
        raise AssertionError("attempted letters must not be sent again")

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", fail_if_letter_is_revalidated)
    monkeypatch.setattr(workflow, "complete_application", fail_if_complete_is_repeated)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["partial_recovery_blocked"] is True
        assert vacancy.data["error_message"] == (
            "Результат отправки письма не подтверждён; повторная отправка заблокирована"
        )
        assert vacancy.data["submission_progress"]["cover_letter_attempted"] is True
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_zarplata_attempted_letter_needs_explicit_reconciler_confirmation(runtime, monkeypatch):
    sessions, session_id = runtime
    attempted_progress = {**partial_progress(), "cover_letter_attempted": True}
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL",
        progress=attempted_progress,
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0
            self.resume_calls = 0

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            self.reconcile_calls += 1
            return {
                "cv_confirmed": True,
                "cover_letter_pending": False,
                "cover_letter_confirmed": True,
                "letter_recovery_safe": False,
            }

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    configure(monkeypatch, adapter)

    async def fail_if_revalidated(*_args, **_kwargs):
        raise AssertionError("explicit site confirmation should not call the model")

    async def fail_if_resumed(*_args, **_kwargs):
        raise AssertionError("confirmed letter should be recorded without a resend")

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", fail_if_revalidated)
    monkeypatch.setattr(workflow, "complete_application", fail_if_resumed)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["submission_progress"]["cover_letter_confirmed"] is True
        assert vacancy.data["submission_progress"]["cover_letter_attempted"] is True
        assert item.counters["submitted"] == 1
        assert item.counters["partial"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is not None
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_zarplata_durable_letter_confirmation_survives_attempt_marker(runtime, monkeypatch):
    sessions, session_id = runtime
    confirmed_progress = {
        "cv_confirmed": True,
        "cover_letter_pending": False,
        "cover_letter_confirmed": True,
        "cover_letter_attempted": True,
    }
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="SUBMITTING",
        progress=confirmed_progress,
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.cv_verify_calls = 0
            self.resume_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="CV already accepted")

        async def verify_cv_submission(self, page):
            self.cv_verify_calls += 1
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            raise AssertionError("durable vacancy letter confirmation needs no re-reconciliation")

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            raise AssertionError("already confirmed letter must not be resumed")

    adapter = Adapter()
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        applications = list(db.scalars(select(Application).where(
            Application.vacancy_id == vacancy_id,
        )))
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["submission_progress"] == confirmed_progress
        assert item.counters["submitted"] == 1
        assert item.counters["partial"] == 0
        assert len(applications) == 1
    assert adapter.cv_verify_calls == adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_zarplata_attempted_letter_cancellation_fences_positive_reconcile(runtime, monkeypatch):
    sessions, session_id = runtime
    attempted_progress = {**partial_progress(), "cover_letter_attempted": True}
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL",
        progress=attempted_progress,
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            self.reconcile_calls += 1
            with sessions() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.STOPPING
                db.commit()
            return {
                "cv_confirmed": True,
                "cover_letter_pending": False,
                "cover_letter_confirmed": True,
            }

        async def resume_application(self, *args, **kwargs):
            raise AssertionError("cancellation must fence all application work")

    adapter = Adapter()
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert item.status == SessionStatus.STOPPING
        assert vacancy.state == "PARTIAL", vacancy.data
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.reconcile_calls == 1


@pytest.mark.asyncio
async def test_zarplata_open_captcha_checkpoints_cv_and_pending_letter(runtime, monkeypatch):
    sessions, session_id = runtime
    seed_zarplata_session(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([JobRef(
                external_id="z-captcha", url="https://zarplata.ru/vacancy/z-captcha",
            )])
            self.open_calls = 0
            self.progress = None

        async def can_retry_application(self, page):
            return True

        async def open_application(self, page):
            self.open_calls += 1
            self.progress = partial_progress()
            raise workflow.CaptchaRequired("CAPTCHA after one-click CV")

        def get_submission_progress(self):
            return self.progress

    adapter = Adapter()
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy).where(
            Vacancy.session_id == session_id, Vacancy.external_id == "z-captcha",
        ))
        assert item.status == SessionStatus.PAUSED
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["submission_progress"] == partial_progress()
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy.id)) is None
    assert adapter.open_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_mode", ["missing", "mismatch"])
async def test_zarplata_oneclick_letter_without_matching_plan_is_blocked_partial(
    runtime, monkeypatch, plan_mode,
):
    sessions, session_id = runtime
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="SUBMITTING", plan_mode=plan_mode,
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0
            self.resume_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="One-click CV accepted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            self.reconcile_calls += 1
            return {**partial_progress(), "letter_recovery_safe": True}

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["partial_recovery_blocked"] is True
        assert vacancy.data["submission_progress"] == partial_progress()
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.reconcile_calls == 0
    assert adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_zarplata_cancel_during_letter_validation_does_not_resume(runtime, monkeypatch):
    sessions, session_id = runtime
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL", progress=partial_progress(),
    )

    class Adapter(FakeAdapter):
        site_id = "zarplata"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.resume_calls = 0

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    configure(monkeypatch, adapter)

    async def stop_during_validation(*_args, **_kwargs):
        with sessions() as db:
            item = db.get(JobSession, session_id)
            item.status = SessionStatus.STOPPING
            db.commit()

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", stop_during_validation)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.STOPPING
        assert db.get(Vacancy, vacancy_id).state == "PARTIAL"
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.resume_calls == 0
