"""Exercise outages and crashes through durable workflow state, without live submissions."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_workflow_non_captcha_continuation import FakeAdapter, evaluation
from test_workflow_non_captcha_continuation import runtime as recovery_runtime

from backend.adapters.base.protocol import ApplicationForm, Blocker, JobRef, SubmissionResult
from backend.orchestrator import workflow
from backend.persistence.models import (
    Application,
    ApplicationPlanRecord,
    BrowserEvent,
    Evaluation,
    JobSession,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.schemas.domain import (
    DesiredJobPolicy,
    FlagMatch,
    JobEvaluation,
    PreferenceFlag,
    SessionStatus,
)
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot

runtime = recovery_runtime


def refs(*ids):
    return [JobRef(external_id=value, url=f"https://fake/{value}") for value in ids]


def configure(monkeypatch, adapter):
    async def apply(*args, **kwargs):
        return evaluation("apply")

    async def letter(*args, **kwargs):
        return "Fixture cover letter"

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "evaluate", apply)
    monkeypatch.setattr(workflow, "write_cover_letter", letter)


@pytest.mark.asyncio
async def test_failed_extraction_is_retried_even_if_listing_loses_the_ref(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        attempts = 0
        searches = 0
        submitted = []

        async def collect_job_refs(self, page):
            self.searches += 1
            return self.refs if self.searches == 1 else []

        async def extract_job(self, page):
            if self.current_ref.external_id == "slow":
                self.attempts += 1
                if self.attempts == 1:
                    raise TimeoutError("slow page")
            return await super().extract_job(page)

        async def submit_application(self, page):
            self.submitted.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs("slow", "healthy"))
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    assert adapter.submitted == ["healthy", "slow"]
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["submitted"] == item.counters["viewed"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "submit"])
async def test_process_crash_after_send_is_reconciled_without_second_click(runtime, monkeypatch, phase):
    server = {"sent": False, "clicks": 0, "verifications": 0}

    class Adapter(FakeAdapter):
        async def can_retry_application(self, page):
            return not server["sent"]

        async def verify_submission(self, page):
            server["verifications"] += 1
            return SubmissionResult(status="already_applied" if server["sent"] else "unknown", message="server state")

        async def open_application(self, page):
            if phase == "open":
                server.update(sent=True, clicks=server["clicks"] + 1)
                raise asyncio.CancelledError()
            return ApplicationForm()

        async def submit_application(self, page):
            server.update(sent=True, clicks=server["clicks"] + 1)
            raise asyncio.CancelledError()

    configure(monkeypatch, Adapter(refs("one")))
    with pytest.raises(asyncio.CancelledError):
        await workflow.WorkflowManager().run(runtime[1])
    with runtime[0]() as db:
        assert db.scalar(select(Vacancy)).state == "SUBMITTING"
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 0

    # New process/adapter: only DB state and site-visible confirmation survive.
    configure(monkeypatch, Adapter(refs("one")))
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).status == SessionStatus.COMPLETED
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
        assert len(list(db.scalars(select(Application)))) == 1
    assert server["clicks"] == 1
    assert server["verifications"] == 1


@pytest.mark.asyncio
async def test_confirmed_submit_reconciles_even_with_corrupted_legacy_private_plan(runtime, monkeypatch):
    server = {"sent": False, "clicks": 0, "verifications": 0}

    class Adapter(FakeAdapter):
        async def can_retry_application(self, page):
            return not server["sent"]

        async def verify_submission(self, page):
            server["verifications"] += 1
            return SubmissionResult(
                status="already_applied" if server["sent"] else "unknown",
                message="server state",
            )

        async def submit_application(self, page):
            server.update(sent=True, clicks=server["clicks"] + 1)
            raise asyncio.CancelledError()

    adapter = Adapter(refs("private-contact"))
    configure(monkeypatch, adapter)
    sessions, session_id = runtime
    with sessions() as db:
        snapshot = _normalize_extracted(
            {
                "external_id": "fixture",
                "identity": {"full_name": "Test", "gender": "male"},
                "contacts": {
                    "phone": "+7 (999) 123-45-67",
                    "messengers": ["https://wa.me/79991234567"],
                },
                "target": {"title": "Role"},
                "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            },
            adapter_id="fake",
            source_url="https://fake/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot)

    with pytest.raises(asyncio.CancelledError):
        await workflow.WorkflowManager().run(session_id)

    with sessions() as db:
        vacancy = db.scalar(select(Vacancy))
        plan = db.scalar(
            select(ApplicationPlanRecord).where(ApplicationPlanRecord.vacancy_id == vacancy.id)
        )
        assert vacancy.state == "SUBMITTING"
        assert plan is not None
        plan.data["cover_letter"] = "WhatsApp: https://wa.me/{{phone}}"
        db.commit()

    configure(monkeypatch, Adapter(refs("private-contact")))
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=5)

    with sessions() as db:
        assert db.get(JobSession, session_id).counters["submitted"] == 1
        assert db.scalar(select(Vacancy)).state == "SUBMITTED"
        assert len(list(db.scalars(select(Application)))) == 1
    assert server == {"sent": True, "clicks": 1, "verifications": 1}


@pytest.mark.asyncio
async def test_submitting_without_verifier_is_error_and_next_vacancy_runs(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        verify_submission = None

    adapter = Adapter(refs("stuck", "healthy"))
    configure(monkeypatch, adapter)
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.status = SessionStatus.RUNNING
        item.counters = {"viewed": 0, "filtered": 0, "matched": 0, "submitted": 0, "errors": 0}
        db.add(
            Vacancy(
                session_id=runtime[1],
                source="fake",
                external_id="stuck",
                url="https://fake/stuck",
                title="Stuck submission",
                state="SUBMITTING",
                data={},
            )
        )
        db.commit()

    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)

    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        rows = {vacancy.external_id: vacancy for vacancy in db.scalars(select(Vacancy))}
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["errors"] == 1
        assert item.counters["submitted"] == 1
        assert rows["stuck"].state == "ERROR"
        assert rows["stuck"].data["error_code"] == "SUBMISSION_UNCONFIRMED"
        assert db.scalar(select(Application).where(Application.vacancy_id == rows["stuck"].id)).status == "unknown"
        assert rows["healthy"].state == "SUBMITTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_blocked_submission_distinguishes_ambiguous_from_confirmed(
    runtime, monkeypatch, confirmed
):
    calls = {"submit": 0}

    class Adapter(FakeAdapter):
        async def submit_application(self, page):
            calls["submit"] += 1
            return SubmissionResult(
                status="blocked", message="site response", confirmed=confirmed
            )

    configure(monkeypatch, Adapter(refs("blocked")))
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=8)

    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        vacancy = db.scalar(select(Vacancy))
        if confirmed:
            assert vacancy.state == "ERROR"
            assert vacancy.data["error_code"] == "SUBMISSION_BLOCKED"
            assert item.counters["errors"] == 1
            assert calls["submit"] == 1
        else:
            assert vacancy.state == "ERROR"
            assert vacancy.data["error_code"] == "SUBMISSION_UNCONFIRMED"
            assert item.counters.get("errors") == 1
            application = db.scalar(select(Application).where(Application.vacancy_id == vacancy.id))
            assert application.status == "unknown"
            assert calls["submit"] == 1


@pytest.mark.asyncio
async def test_form_timeout_before_send_retries_when_site_confirms_no_application(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        attempts = 0

        async def can_retry_application(self, page):
            return True

        async def open_application(self, page):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("form unavailable")
            return ApplicationForm()

    adapter = Adapter(refs("one"))
    configure(monkeypatch, adapter)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    assert adapter.attempts == 2
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1


@pytest.mark.asyncio
async def test_captcha_inside_form_pauses_without_sending(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        captcha = False
        submitted = False

        async def open_application(self, page):
            self.captcha = True
            return ApplicationForm()

        async def detect_blockers(self, page):
            return [Blocker(kind="captcha", message="captcha")] if self.captcha else []

        async def submit_application(self, page):
            self.submitted = True
            return await super().submit_application(page)

    adapter = Adapter(refs("one"))
    configure(monkeypatch, adapter)
    await workflow.WorkflowManager().run(runtime[1])
    assert not adapter.submitted
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).status == SessionStatus.PAUSED
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 0


@pytest.mark.asyncio
async def test_stop_interrupts_backoff_and_preserves_user_stop(runtime, monkeypatch):
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 60
    calls = 0

    async def fail(session_id):
        nonlocal calls
        calls += 1
        raise workflow.ModelUnavailable("offline")

    monkeypatch.setattr(manager, "_run", fail)
    task = asyncio.create_task(manager.run(runtime[1]))
    for _ in range(100):
        with runtime[0]() as db:
            item = db.get(JobSession, runtime[1])
            if (item.recovery or {}).get("retry_at"):
                assert item.status == SessionStatus.RUNNING
                item.status = SessionStatus.STOPPED
                item.stop_reason = "user stop"
                db.commit()
                break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(task, timeout=1)
    assert calls == 1
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).stop_reason == "user stop"


@pytest.mark.asyncio
async def test_recovery_budget_exhaustion_fails_stalled_session(runtime, monkeypatch):
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 0
    manager.retry_max_seconds = 0
    calls = 0

    async def no_progress(_session_id):
        nonlocal calls
        calls += 1
        raise workflow.RecoverableFailure("same stage remains pending")

    monkeypatch.setattr(manager, "_run", no_progress)
    await asyncio.wait_for(manager.run(runtime[1]), timeout=3)

    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        events = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "session_failed",
        )))
        assert item.status == SessionStatus.FAILED
        assert item.recovery["attempt"] == workflow._SESSION_RECOVERY_RETRY_LIMIT
        assert calls == workflow._SESSION_RECOVERY_RETRY_LIMIT + 1
        assert events[-1].data == {
            "kind": "recovery_exhausted",
            "attempts": workflow._SESSION_RECOVERY_RETRY_LIMIT,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [workflow.ModelUnavailable, workflow.AuthenticationPending])
async def test_external_dependency_recovery_is_unbounded_and_model_backoff_grows(runtime, error_type):
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 0.001
    manager.retry_max_seconds = 10
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.status = SessionStatus.RUNNING
        item.recovery = {"attempt": workflow._SESSION_RECOVERY_RETRY_LIMIT}
        db.commit()

    for _ in range(workflow._SESSION_RECOVERY_RETRY_LIMIT + 3):
        assert await manager._recover(runtime[1], error_type("dependency unavailable"))

    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "recovery_retry",
        )))
        failures = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "session_failed",
        )))
        assert item.status == SessionStatus.RUNNING
        delays = [event.data["delay_seconds"] for event in retries]
        if error_type is workflow.ModelUnavailable:
            assert delays == [
                min(manager.retry_max_seconds, manager.retry_base_seconds * 2 ** min(index, 10))
                for index in range(workflow._SESSION_RECOVERY_RETRY_LIMIT, workflow._SESSION_RECOVERY_RETRY_LIMIT + len(delays))
            ]
        else:
            assert all(delay == manager.retry_base_seconds for delay in delays)
        assert not failures


@pytest.mark.asyncio
async def test_recovery_budget_resets_only_after_counter_progress(runtime):
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 0
    manager.retry_max_seconds = 0
    error = workflow.RecoverableFailure("same stage remains pending")
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.status = SessionStatus.RUNNING
        db.commit()

    assert await manager._recover(runtime[1], error)
    assert await manager._recover(runtime[1], error)
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).recovery["attempt"] == 2
        db.get(JobSession, runtime[1]).counters = {"viewed": 1}
        db.commit()
    assert await manager._recover(runtime[1], error)
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        assert item.recovery["attempt"] == 1
        assert item.recovery[workflow._RECOVERY_COUNTERS_KEY] == {"viewed": 1}


@pytest.mark.asyncio
async def test_clean_pass_clears_recovery_progress_snapshot(runtime, monkeypatch):
    configure(monkeypatch, FakeAdapter(refs("one")))
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.recovery = {
            "attempt": 3,
            workflow._RECOVERY_COUNTERS_KEY: {"viewed": 0},
        }
        db.commit()
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    with runtime[0]() as db:
        recovery = db.get(JobSession, runtime[1]).recovery
        assert recovery["attempt"] == 0
        assert workflow._RECOVERY_COUNTERS_KEY not in recovery


@pytest.mark.asyncio
async def test_model_outage_retries_saved_vacancy_without_reopening_search(runtime, monkeypatch):
    class Adapter(FakeAdapter):
        searches = 0
        opens = 0

        async def open_search(self, page, filters):
            self.searches += 1
            return await super().open_search(page, filters)

        async def open_job(self, page, ref):
            self.opens += 1
            return await super().open_job(page, ref)

    adapter = Adapter(refs("recover-in-place", "healthy"))
    configure(monkeypatch, adapter)
    calls = 0

    async def flaky_evaluation(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelUnavailable("temporary outage")
        return evaluation("apply")

    monkeypatch.setattr(workflow, "evaluate", flaky_evaluation)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)

    with runtime[0]() as db:
        vacancies = {
            row.external_id: row
            for row in db.scalars(select(Vacancy).where(Vacancy.session_id == runtime[1]))
        }
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "model_stage_retry",
        )))
        submissions = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "submission",
        ).order_by(BrowserEvent.id)))
        assert vacancies["recover-in-place"].state == "SUBMITTED"
        assert vacancies["healthy"].state == "SUBMITTED"
        assert vacancies["recover-in-place"].data.get("model_retry_budgets") is None
        assert len(retries) == 1
        assert retries[0].data["stage"] == "evaluation"
        healthy_submit = next(
            event for event in submissions
            if event.data.get("vacancy_id") == vacancies["healthy"].id
        )
        delayed_submit = next(
            event for event in submissions
            if event.data.get("vacancy_id") == vacancies["recover-in-place"].id
        )
        assert retries[0].id < healthy_submit.id < delayed_submit.id
    assert calls == 3
    assert adapter.searches == 1


@pytest.mark.asyncio
async def test_permanent_model_error_finishes_only_its_vacancy(runtime, monkeypatch):
    adapter = FakeAdapter(refs("permanent", "healthy"))
    configure(monkeypatch, adapter)
    calls = 0

    async def one_permanent_error(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelPermanentError("invalid model output")
        return evaluation("apply")

    monkeypatch.setattr(workflow, "evaluate", one_permanent_error)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)

    with runtime[0]() as db:
        vacancies = {
            row.external_id: row
            for row in db.scalars(select(Vacancy).where(Vacancy.session_id == runtime[1]))
        }
        assert vacancies["permanent"].state == "ERROR"
        assert vacancies["healthy"].state == "SUBMITTED"
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_hh_letter_retry_resumes_after_confirmed_cv_without_second_response(runtime, monkeypatch):
    class HHAdapter(FakeAdapter):
        site_id = "hh"

        def __init__(self, jobs):
            super().__init__(jobs)
            self.cv_clicks = 0
            self.letter_resumes = 0
            self.progress = {
                "cv_confirmed": False,
                "cover_letter_pending": False,
                "cover_letter_confirmed": False,
            }

        async def open_application(self, page):
            self.cv_clicks += 1
            self.progress = {
                "cv_confirmed": True,
                "cover_letter_pending": True,
                "cover_letter_confirmed": False,
            }
            return ApplicationForm()

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def resume_application(
            self, page, plan, *, cv_confirmed, cover_letter_pending
        ):
            assert cv_confirmed and cover_letter_pending
            self.letter_resumes += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return dict(self.progress)

    sessions, session_id = runtime
    adapter = HHAdapter(refs("cv-and-letter"))
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.recovery = {"search_filters": {}}
        snapshot = _normalize_extracted(
            {
                "external_id": "fixture",
                "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"},
                "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            },
            adapter_id="hh",
            source_url="https://hh/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()

    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    application_calls = 0

    async def interrupted_then_resumed(*_args, **_kwargs):
        nonlocal application_calls
        application_calls += 1
        if application_calls == 1:
            raise workflow.ModelUnavailable("letter stage temporarily unavailable")
        adapter.progress = {
            "cv_confirmed": True,
            "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return type("Outcome", (), {
            "submission": SubmissionResult(status="submitted", message="letter confirmed"),
            "stopped": False,
            "error_code": None,
            "error_message": None,
        })()

    monkeypatch.setattr(workflow, "complete_application", interrupted_then_resumed)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=5)

    with sessions() as db:
        vacancy = db.scalar(select(Vacancy).where(Vacancy.session_id == session_id))
        saved_plan = db.scalar(select(ApplicationPlanRecord).where(
            ApplicationPlanRecord.vacancy_id == vacancy.id
        ))
        assert vacancy.state == "SUBMITTED", (
            vacancy.state, vacancy.data.get("error_code"),
            vacancy.data.get("error_message"), vacancy.data.get("submission_progress"),
            saved_plan.data.get(workflow._RESUME_HASH_KEY) if saved_plan else None,
            workflow._snapshot_content_hash(db.scalar(select(SessionResumeSnapshot).where(
                SessionResumeSnapshot.session_id == session_id
            ))),
            adapter.cv_clicks, adapter.letter_resumes, application_calls,
            [
                (event.event_type, event.data)
                for event in db.scalars(select(BrowserEvent).where(
                    BrowserEvent.session_id == session_id,
                ).order_by(BrowserEvent.id))
            ],
        )
        assert vacancy.data["submission_progress"] == {
            "cv_confirmed": True,
            "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
    assert application_calls == 2
    assert adapter.cv_clicks == 1
    assert adapter.letter_resumes == 1


@pytest.mark.parametrize(
    ("session_status", "error_code"),
    [
        (SessionStatus.STOPPED, "SESSION_STOPPED"),
        (SessionStatus.FAILED, "SESSION_FAILED"),
        (SessionStatus.COMPLETED, "VACANCY_PROCESSING_FAILED"),
    ],
)
def test_terminal_cleanup_closes_processing_vacancies_idempotently(
    runtime, session_status, error_code
):
    sessions, session_id = runtime
    pending_states = [
        "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING",
    ]
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.status = session_status
        item.counters = {"errors": 2}
        db.add_all([
            Vacancy(
                session_id=session_id,
                source="fake",
                external_id=f"pending-{index}",
                url=f"https://fake/pending-{index}",
                title="Pending",
                state=state,
                data={},
            )
            for index, state in enumerate(pending_states)
        ])
        db.commit()
        manager = workflow.WorkflowManager()
        assert manager._terminalize_pending_vacancies(db, item) == len(pending_states)
        db.commit()
        assert manager._terminalize_pending_vacancies(db, item) == 0
        db.commit()
        rows = list(db.scalars(select(Vacancy).where(Vacancy.session_id == session_id)))
        assert {row.state for row in rows} == {"ERROR"}
        assert {row.data["error_code"] for row in rows} == {error_code}
        assert item.counters["errors"] == 2 + len(pending_states)


def test_save_refs_prioritizes_all_started_vacancies_before_new_refs(runtime):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.recovery = {
            "pending_refs": [
                {"external_id": "new-one", "url": "https://fake/new-one"},
                {"external_id": "new-two", "url": "https://fake/new-two"},
            ]
        }
        db.add_all([
            Vacancy(
                session_id=session_id,
                source="fake",
                external_id=external_id,
                url=f"https://fake/{external_id}",
                title="Started",
                state=state,
                data={},
            )
            for external_id, state in [
                ("old-evaluating", "EVALUATING"),
                ("old-ready", "READY_TO_SUBMIT"),
                ("old-submitting", "SUBMITTING"),
            ]
        ])
        db.commit()

    queued = workflow.WorkflowManager()._save_refs(
        session_id,
        refs("new-one", "new-two"),
    )

    assert [ref.external_id for ref in queued] == [
        "old-submitting", "old-evaluating", "old-ready", "new-one", "new-two"
    ]


@pytest.mark.asyncio
async def test_submission_reconciliation_prioritizes_first_ref_and_retries_immediately(
    runtime, monkeypatch
):
    events = []

    class Adapter(FakeAdapter):
        first_submission = True

        async def can_retry_application(self, page):
            events.append(("can_retry", self.current_ref.external_id))
            return self.current_ref.external_id == "first" and not self.first_submission

        async def verify_submission(self, page):
            events.append(("verify", self.current_ref.external_id))
            return SubmissionResult(status="unknown", message="нет подтверждения")

        async def submit_application(self, page):
            external_id = self.current_ref.external_id
            events.append(("submit", external_id))
            if external_id == "first" and self.first_submission:
                self.first_submission = False
                return SubmissionResult(status="unknown", message="таймаут")
            return SubmissionResult(status="submitted", message="отправлено")

    async def apply(*args, **kwargs):
        job = next(value for value in args if hasattr(value, "external_id"))
        events.append(("evaluate", job.external_id))
        return evaluation("apply")

    async def letter(*args, **kwargs):
        return "Fixture cover letter"

    adapter = Adapter(refs("first", "second"))
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "evaluate", apply)
    monkeypatch.setattr(workflow, "write_cover_letter", letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)

    submissions = [event for event in events if event[0] == "submit"]
    assert submissions == [("submit", "first"), ("submit", "first"), ("submit", "second")]
    first_retry = events.index(("submit", "first"), events.index(("submit", "first")) + 1)
    second_ref = next(index for index, event in enumerate(events) if event == ("submit", "second"))
    second_evaluation = next(index for index, event in enumerate(events) if event == ("evaluate", "second"))
    assert any(event == ("verify", "first") for event in events[:first_retry])
    assert first_retry < second_ref
    assert first_retry < second_evaluation


def test_startup_recovers_only_accepted_active_work(runtime, monkeypatch):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.status = SessionStatus.RUNNING
        for status in (SessionStatus.CREATED, SessionStatus.PAUSED, SessionStatus.STOPPED, SessionStatus.COMPLETED):
            db.add(JobSession(adapter_id="fake", status=status))
        db.commit()
    launched = []
    monkeypatch.setattr(workflow.workflow_manager, "launch", lambda ident: launched.append(ident))
    assert workflow.recover_orphaned_sessions() == [session_id]
    assert launched == [session_id]


@pytest.mark.asyncio
async def test_browser_is_automatically_restored(runtime, monkeypatch):
    adapter = FakeAdapter(refs("one"))
    configure(monkeypatch, adapter)
    restored = []

    async def restore(session_id, adapter):
        restored.append(session_id)
        return SimpleNamespace(page=object())

    monkeypatch.setattr(workflow, "get_browser", lambda _: None)
    monkeypatch.setattr(workflow, "restore_browser", restore)
    await workflow.WorkflowManager().run(runtime[1])
    assert restored == [runtime[1]]


@pytest.mark.asyncio
async def test_legacy_cached_apply_with_red_flags_is_re_evaluated_without_submission(runtime, monkeypatch):
    sessions, session_id = runtime
    submitted = []
    calls = []

    class Adapter(FakeAdapter):
        async def submit_application(self, page):
            submitted.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs("legacy"))
    policy = DesiredJobPolicy(
        red_flags=[PreferenceFlag(id="red-1", text="ГПХ", category="other")],
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.status = SessionStatus.RUNNING
        item.started_at = workflow.datetime.now(workflow.timezone.utc)
        item.preference_policy = policy.model_dump(mode="json")
        item.desired_job_description = "Не рассматриваю ГПХ"
        item.counters = {"viewed": 0, "filtered": 0, "matched": 3, "submitted": 0, "reported": 0, "errors": 0}
        vacancy = Vacancy(
            session_id=session_id, source="fake", external_id="legacy", url="https://fake/legacy",
            title="Vacancy legacy", state="EXTRACTED",
            data={"source": "fake", "external_id": "legacy", "url": "https://fake/legacy",
                  "title": "Vacancy legacy", "description": "Description", "responsibilities": [],
                  "required_skills": [], "optional_skills": []},
        )
        db.add(vacancy)
        db.flush()
        old_result = JobEvaluation(
            decision="apply", score=90, confidence=0.9, category="legacy", reason="legacy",
            flag_matches=[FlagMatch(flag_id="red-1", matched=False, confidence=0, evidence=[])],
        )
        db.add(Evaluation(vacancy_id=vacancy.id, data=old_result.model_dump()))
        db.commit()

    async def re_evaluate(*args, **kwargs):
        calls.append(args[-1] if len(args) >= 6 else kwargs.get("preference_policy"))
        return JobEvaluation(decision="skip", score=10, confidence=1, category="legacy", reason="red flag")

    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _: adapter)
    monkeypatch.setattr(workflow, "evaluate", re_evaluate)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=5)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        saved = db.scalar(select(Evaluation).join(Vacancy).where(Vacancy.external_id == "legacy"))
        assert item.counters["matched"] == 3
        assert db.scalar(select(Vacancy).where(Vacancy.external_id == "legacy")).state == "REJECTED_BY_MODEL"
        assert JobEvaluation.model_validate(saved.data).decision == "skip"
    assert calls and calls[0].red_flags[0].id == "red-1"
    assert submitted == []


@pytest.mark.asyncio
async def test_target_reached_does_not_depend_on_model_or_browser(runtime, monkeypatch):
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.started_at = workflow.datetime.now(workflow.timezone.utc)
        item.application_limit = 100
        item.counters = {"submitted": 100}
        db.commit()
    monkeypatch.setattr(workflow, "get_browser", lambda _: pytest.fail("already at goal"))
    await workflow.WorkflowManager().run(runtime[1])
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["errors"] == 0


@pytest.mark.asyncio
async def test_preference_compilation_outage_is_retried_inside_workflow(runtime, monkeypatch):
    from backend.schemas.domain import DesiredJobPolicy

    configure(monkeypatch, FakeAdapter(refs("one")))
    with runtime[0]() as db:
        db.get(JobSession, runtime[1]).desired_job_description = "GameDev"
        db.commit()
    calls = 0

    async def compile_policy(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelUnavailable("offline")
        return DesiredJobPolicy()

    monkeypatch.setattr(workflow, "compile_preference_policy", compile_policy)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).preference_policy is not None
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
        assert len(list(db.scalars(select(BrowserEvent).where(BrowserEvent.event_type == "recovery_retry")))) == 1
    assert calls == 2
