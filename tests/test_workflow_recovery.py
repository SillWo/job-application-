"""Exercise outages and crashes through durable workflow state, without live submissions."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_workflow_non_captcha_continuation import FakeAdapter, evaluation, run_workflow
from test_workflow_non_captcha_continuation import runtime as recovery_runtime

from backend.adapters.base.protocol import ApplicationForm, Blocker, JobRef, SubmissionResult
from backend.orchestrator import workflow
from backend.orchestrator.terminal_finalization import scrub_snapshot_question_artifacts
from backend.persistence.models import (
    Application,
    ApplicationPlanRecord,
    BrowserEvent,
    CoverLetter,
    Evaluation,
    JobSession,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.schemas.domain import (
    ApplicationPlan,
    DesiredJobPolicy,
    FlagMatch,
    JobEvaluation,
    PreferenceFlag,
    SessionStatus,
)
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot

runtime = recovery_runtime


def test_terminal_cleanup_preserves_partial_application_recovery_artifacts(runtime):
    sessions, session_id = runtime
    question_data = {"known_answers": {"Are you eligible?": "yes"}}
    plan_data = {"form_answers": {"Are you eligible?": "yes"}, "step": "cover_letter"}
    with sessions() as db:
        partial = Vacancy(
            session_id=session_id, source="hh", external_id="partial",
            url="https://fake/partial", title="Partial", state="PARTIAL",
            data=question_data,
        )
        terminal = Vacancy(
            session_id=session_id, source="hh", external_id="terminal",
            url="https://fake/terminal", title="Terminal", state="ERROR",
            data=question_data,
        )
        db.add_all([partial, terminal])
        db.flush()
        partial_plan = ApplicationPlanRecord(vacancy_id=partial.id, data=plan_data)
        terminal_plan = ApplicationPlanRecord(vacancy_id=terminal.id, data=plan_data)
        db.add_all([partial_plan, terminal_plan])
        db.commit()
        partial_id, terminal_id = partial.id, terminal.id

        scrub_snapshot_question_artifacts(db, session_id)
        assert db.get(Vacancy, partial_id).data == question_data
        assert db.scalar(select(ApplicationPlanRecord).where(
            ApplicationPlanRecord.vacancy_id == partial_id
        )).data == plan_data
        assert db.get(Vacancy, terminal_id).data == {}
        assert db.scalar(select(ApplicationPlanRecord).where(
            ApplicationPlanRecord.vacancy_id == terminal_id
        )).data == {"step": "cover_letter"}


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
async def test_cross_session_errors_and_rejections_retry_but_external_progress_is_suppressed(
    runtime, monkeypatch,
):
    sessions, session_id = runtime
    ids = ("old-error", "old-rejected", "old-submitted", "old-uncertain")

    class Adapter(FakeAdapter):
        site_id = "hh"

        def __init__(self, jobs):
            super().__init__(jobs)
            self.extracted_ids = []
            self.submitted_ids = []

        async def extract_job(self, page):
            self.extracted_ids.append(self.current_ref.external_id)
            return await super().extract_job(page)

        async def submit_application(self, page):
            self.submitted_ids.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs(*ids))
    with sessions() as db:
        old_session = JobSession(
            adapter_id="hh", status=SessionStatus.COMPLETED,
            counters={}, application_limit=None,
        )
        db.add(old_session)
        db.flush()
        for external_id, state, data in (
            ("old-error", "ERROR", {"error_code": "MODEL_TIMEOUT"}),
            ("old-rejected", "REJECTED_BY_MODEL", {}),
            ("old-submitted", "SUBMITTED", {}),
            ("old-uncertain", "ERROR", {"submission_attempted": True}),
        ):
            vacancy = Vacancy(
                session_id=old_session.id, source="hh", external_id=external_id,
                url=f"https://fake/{external_id}", title=external_id,
                state=state, data=data,
            )
            db.add(vacancy)
            db.flush()
            if external_id == "old-rejected":
                db.add(Evaluation(vacancy_id=vacancy.id, data={"decision": "skip", "score": 5}))
            if external_id == "old-submitted":
                db.add(Application(vacancy_id=vacancy.id, status="submitted"))

        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.application_limit = None
        item.recovery = {"search_filters": {}}
        persist_session_snapshot(db, session_id, _normalize_extracted(
            {
                "external_id": "current", "identity": {"full_name": "Fixture"},
                "target": {"title": "Role"}, "about": "Synthetic candidate",
                "skills": [{"name": "Python"}],
            }, adapter_id="hh", source_url="https://fake/resume/current",
        ))
        db.commit()

    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        old = {row.external_id: row for row in db.scalars(select(Vacancy).where(
            Vacancy.session_id == old_session.id,
        ))}
        current = {row.external_id: row for row in db.scalars(select(Vacancy).where(
            Vacancy.session_id == session_id,
        ))}
        assert set(current) == set(ids)
        assert current["old-error"].state == "SUBMITTED"
        assert current["old-rejected"].state == "SUBMITTED"
        assert current["old-submitted"].state == "ALREADY_APPLIED"
        assert current["old-uncertain"].state == "SUBMISSION_UNCONFIRMED"
        assert adapter.extracted_ids == ["old-error", "old-rejected"]
        assert adapter.submitted_ids == ["old-error", "old-rejected"]
        assert old["old-error"].state == "ERROR"
        assert old["old-rejected"].state == "REJECTED_BY_MODEL"
        assert old["old-submitted"].state == "SUBMITTED"
        assert old["old-uncertain"].data == {"submission_attempted": True}
        assert db.scalar(select(Application).where(Application.vacancy_id == old["old-submitted"].id)).status == "submitted"
        assert len(list(db.scalars(select(Application)))) == 3


@pytest.mark.asyncio
async def test_permanent_model_error_fails_session_without_retry(runtime):
    manager = workflow.WorkflowManager()
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.status = SessionStatus.RUNNING
        db.commit()

    assert not await manager._recover(
        runtime[1], workflow.ModelPermanentError("sensitive provider detail", error_code="invalid_output")
    )
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        events = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "session_failed",
        )))
        assert item.status == SessionStatus.FAILED
        assert "sensitive provider detail" not in item.stop_reason
        assert item.recovery.get("attempt", 0) == 0
        assert events[-1].data == {"kind": "permanent_model_error", "error_code": "invalid_output"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_setting",
    [
        None,
        ("desired_job_description", "Product manager"),
        ("minimum_scores", {"overall": 80}),
        ("guaranteed_application", True),
        ("cover_letter_auto", False),
        ("cover_letter_template", "New template"),
        ("cover_letter_max_words", 150),
        "missing-source-session",
    ],
    ids=["same-settings", "criteria", "minimum-scores", "guaranteed-application",
         "cover-letter-auto", "cover-letter-template", "cover-letter-limit",
         "missing-source-session"],
)
async def test_cross_session_partial_uses_original_plan_only_for_same_resume(
    runtime, monkeypatch, changed_setting,
):
    sessions, session_id = runtime
    external_id = "old-partial"
    snapshot = _normalize_extracted(
        {
            "external_id": "current", "identity": {"full_name": "Fixture"},
            "target": {"title": "Role"}, "about": "Synthetic candidate",
            "skills": [{"name": "Python"}],
        }, adapter_id="hh", source_url="https://fake/resume/current",
    )

    class Adapter(FakeAdapter):
        site_id = "hh"

        def __init__(self, jobs):
            super().__init__(jobs)
            self.progress = {
                "cv_confirmed": True, "cover_letter_pending": True,
                "cover_letter_confirmed": False,
            }
            self.resume_calls = 0
            self.cv_upload_calls = 0
            self.verify_calls = 0
            self.submitted_ids = []

        async def verify_cv_submission(self, page):
            self.verify_calls += 1
            return SubmissionResult(status="submitted", message="confirmed CV")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            assert cv_confirmed and cover_letter_pending
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return dict(self.progress)

        async def submit_application(self, page):
            self.submitted_ids.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs(external_id, "healthy") if changed_setting else refs(external_id))
    completed_ids = []
    recovery_evaluation_calls = []
    with sessions() as db:
        old_session = JobSession(
            adapter_id="hh", status=SessionStatus.COMPLETED,
            counters={}, application_limit=None,
            desired_job_description=" Senior Python ", minimum_scores={},
        )
        db.add(old_session)
        db.flush()
        persist_session_snapshot(db, old_session.id, snapshot)
        old_resume_hash = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == old_session.id,
        )).content_hash
        old = Vacancy(
            session_id=None if changed_setting == "missing-source-session" else old_session.id,
            source="hh", external_id=external_id,
            url=f"https://fake/{external_id}", title=external_id, state="PARTIAL",
            data={
                "source": "hh", "external_id": external_id, "url": f"https://fake/{external_id}",
                "title": external_id, "description": "Description", "responsibilities": [],
                "required_skills": [], "optional_skills": [], "submission_attempted": True,
            "partial_counted": True, "partial_error_counted": True,
                "submission_progress": {
                    "cv_confirmed": True, "cover_letter_pending": True,
                    "cover_letter_confirmed": False,
                },
            },
        )
        db.add(old)
        db.flush()
        old_vacancy_data = dict(old.data)
        plan = ApplicationPlan(
            vacancy_id=old.id, resume_file="fixture.pdf",
            cover_letter="Синтетическое письмо для продолжения отклика.",
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = old_resume_hash
        db.add(ApplicationPlanRecord(vacancy_id=old.id, data=plan))

        current = db.get(JobSession, session_id)
        current.adapter_id = "hh"
        current.application_limit = 1 if changed_setting is None else None
        current.desired_job_description = "Senior Python "
        current.minimum_scores = None
        current.preference_policy = DesiredJobPolicy(
            contract_version=workflow.POLICY_CONTRACT_VERSION,
            green_flags=(
                [PreferenceFlag(
                    id="required-recovery-role", text="Required synthetic role",
                    category="desired_task", required=True, source_quote="Description",
                )]
                if changed_setting is None else []
            ),
        ).model_dump(mode="json")
        if isinstance(changed_setting, tuple):
            setattr(current, *changed_setting)
        current.recovery = {"search_filters": {}}
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()

    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    if changed_setting is None:
        async def fresh_required_evaluation(*_args, **_kwargs):
            recovery_evaluation_calls.append("fresh")
            return evaluation("apply")

        monkeypatch.setattr(workflow, "evaluate", fresh_required_evaluation)

    async def accept_synthetic_original_letter(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", accept_synthetic_original_letter)

    async def finish_letter(*_args, **_kwargs):
        completed_ids.append("called")
        adapter.progress = {
            "cv_confirmed": True, "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="letter confirmed"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "complete_application", finish_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        rows = list(db.scalars(select(Vacancy).where(Vacancy.external_id == external_id).order_by(Vacancy.id)))
        current_row = next(row for row in rows if row.session_id == session_id)
        old_source_session_id = None if changed_setting == "missing-source-session" else old_session.id
        old_row = next(row for row in rows if row.session_id == old_source_session_id)
        old_plan_row = db.scalar(select(ApplicationPlanRecord).where(
            ApplicationPlanRecord.vacancy_id == old_row.id,
        ))
        if changed_setting is None:
            plan_row = db.scalar(select(ApplicationPlanRecord).where(
                ApplicationPlanRecord.vacancy_id == current_row.id,
            ))
            assert current_row.state == "SUBMITTED"
            assert current_row.data["historical_source_vacancy_id"] == old_row.id
            assert current_row.data[workflow._LETTER_CLAIMS_PROOF_KEY]["version"] == workflow.LETTER_CLAIMS_VERSION
            assert current_row.data["submission_progress"]["cover_letter_confirmed"] is True
            assert not current_row.data.get("partial_counted")
            assert not current_row.data.get("partial_error_counted")
            assert db.get(JobSession, session_id).counters["partial"] == 0
            assert recovery_evaluation_calls == ["fresh"]
            assert plan_row.data["vacancy_id"] == current_row.id
            assert db.scalar(select(Evaluation).where(
                Evaluation.vacancy_id == current_row.id,
            )).data[workflow._EVALUATION_FINGERPRINT_KEY]
            assert plan_row.data["vacancy_id"] == current_row.id
            assert old_row.state == "PARTIAL"
            assert adapter.resume_calls == 1
            assert adapter.verify_calls == 1
            assert adapter.cv_upload_calls == 0
            assert completed_ids == ["called"]
            assert db.scalar(select(Application).where(Application.vacancy_id == current_row.id)) is not None
            assert db.scalar(select(Application).where(Application.vacancy_id == old_row.id)) is None
        else:
            healthy_row = db.scalar(select(Vacancy).where(
                Vacancy.session_id == session_id, Vacancy.external_id == "healthy",
            ))
            assert current_row.state == "PARTIAL"
            assert current_row.data["partial_recovery_blocked"] is True
            assert current_row.data["submission_progress"]["cv_confirmed"] is True
            assert current_row.data["submission_progress"]["cover_letter_pending"] is True
            assert db.scalar(select(ApplicationPlanRecord).where(
                ApplicationPlanRecord.vacancy_id == current_row.id,
            )) is None
            assert healthy_row is not None and healthy_row.state == "SUBMITTED"
            assert completed_ids == ["called"]
            assert db.scalar(select(Application).where(Application.vacancy_id == healthy_row.id)) is not None
            assert adapter.resume_calls == 0
            assert adapter.verify_calls == 0
            assert adapter.cv_upload_calls == 0
            assert old_row.state == "PARTIAL"
            assert old_row.data == old_vacancy_data
            assert old_plan_row.data == plan
        assert old_row.state == "PARTIAL"
        assert db.scalar(select(Application).where(Application.vacancy_id == old_row.id)) is None


@pytest.mark.asyncio
async def test_legacy_error_partial_is_rebuilt_after_restart_and_resumes_saved_letter(runtime, monkeypatch):
    sessions, session_id = runtime
    external_id = "legacy-partial"

    class Adapter(FakeAdapter):
        site_id = "hh"

        def __init__(self):
            super().__init__([])
            self.progress = {
                "cv_confirmed": True, "cover_letter_pending": True,
                "cover_letter_confirmed": False,
            }
            self.verify_calls = 0
            self.resume_calls = []
            self.cv_upload_calls = 0

        async def verify_cv_submission(self, page):
            self.verify_calls += 1
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            assert cv_confirmed and cover_letter_pending
            self.resume_calls.append(plan.cover_letter)
            return ApplicationForm()

        def get_submission_progress(self):
            return dict(self.progress)

        def next_retry_delay(self):
            return 0

    adapter = Adapter()
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.status = SessionStatus.RUNNING
        item.application_limit = 1
        item.counters = {
            "viewed": 1, "filtered": 0, "matched": 1, "submitted": 0,
            "partial": 0, "errors": 0,
        }
        persist_session_snapshot(db, session_id, _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            }, adapter_id="hh", source_url="https://hh.ru/resume/fixture",
        ))
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy = Vacancy(
            session_id=session_id, source="hh", external_id=external_id,
            url=f"https://hh.ru/vacancy/{external_id}", title="Legacy partial",
            state="ERROR",
            data={
                "source": "hh", "external_id": external_id,
                "url": f"https://hh.ru/vacancy/{external_id}",
                "title": "Legacy partial", "description": "Synthetic posting description",
                "responsibilities": [], "required_skills": [], "optional_skills": [],
                "error_code": "VACANCY_PROCESSING_FAILED",
                "submission_progress": dict(adapter.progress),
            },
        )
        db.add(vacancy)
        db.flush()
        saved_plan = ApplicationPlan(
            vacancy_id=vacancy.id, resume_file="fixture.pdf",
            cover_letter="Durable letter from the original plan",
        ).model_dump()
        saved_plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=saved_plan))
        # Simulate restart after the legacy queue entry was lost.
        item.recovery = {"search_filters": {"portfolio_queries": []}}
        db.commit()

    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)

    async def accept_synthetic_durable_letter(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", accept_synthetic_durable_letter)

    async def finish_letter(_adapter, _page, plan, *_args, **_kwargs):
        assert plan.cover_letter == "Durable letter from the original plan"
        adapter.progress = {
            "cv_confirmed": True, "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="letter confirmed"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "complete_application", finish_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy).where(
            Vacancy.session_id == session_id, Vacancy.external_id == external_id,
        ))
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["submitted"] == 1
        assert item.counters["partial"] == 0
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["partial_letter_attempts"] == 1
        assert adapter.verify_calls == 1
        assert adapter.resume_calls == ["Durable letter from the original plan"]
        assert adapter.cv_upload_calls == 0


def test_letter_claim_proof_is_invalidated_by_letter_resume_or_checker_version(runtime):
    _sessions, session_id = runtime
    letter = "Synthetic letter"
    proof = workflow._letter_claims_proof(letter, [], "resume-v1")
    vacancy = Vacancy(session_id=session_id, source="fake", external_id="proof", data={
        workflow._LETTER_CLAIMS_PROOF_KEY: proof,
    })

    assert workflow._letter_claims_proof_matches(vacancy, letter, [], "resume-v1")
    assert not workflow._letter_claims_proof_matches(vacancy, "Changed letter", [], "resume-v1")
    assert not workflow._letter_claims_proof_matches(vacancy, letter, [], "resume-v2")
    vacancy.data[workflow._LETTER_CLAIMS_PROOF_KEY]["version"] = "old-checker"
    assert not workflow._letter_claims_proof_matches(vacancy, letter, [], "resume-v1")


def test_evaluation_fingerprint_tracks_broker_prompt_version(runtime, monkeypatch):
    sessions, session_id = runtime
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        initial = workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        )
        assert workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        ) == initial
        monkeypatch.setattr(workflow, "BROKER_PROMPT_VERSION", "test-next-prompt-version")
        changed = workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        )
        assert changed != initial
        assert workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        ) == changed


def test_evaluation_fingerprint_invalidates_previous_evaluation_contract(runtime, monkeypatch):
    sessions, session_id = runtime
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        current = workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        )
        assert workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        ) == current
        monkeypatch.setattr(workflow, "_EVALUATION_CONTRACT", "job-evaluation-v2")
        previous = workflow._evaluation_fingerprint(
            db, snapshot.content_hash, {}, None, object(),
        )
        assert previous != current


@pytest.mark.asyncio
async def test_cached_unproven_letter_is_rejected_before_submission_and_other_jobs_continue(
    runtime, monkeypatch,
):
    sessions, session_id = runtime
    letter = "Unsupported synthetic candidate facts"

    class Adapter(FakeAdapter):
        def __init__(self, jobs):
            super().__init__(jobs)
            self.application_opens = []
            self.submitted_ids = []

        async def open_application(self, page):
            self.application_opens.append(self.current_ref.external_id)
            return await super().open_application(page)

        async def submit_application(self, page):
            self.submitted_ids.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs("cached-invalid", "healthy-after-invalid"))
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy = Vacancy(
            session_id=session_id, source="fake", external_id="cached-invalid",
            url="https://fake/cached-invalid", title="Cached invalid", state="EVALUATING",
            data={workflow._COVER_LETTER_HASH_KEY: snapshot.content_hash},
        )
        db.add(vacancy)
        db.flush()
        plan = ApplicationPlan(
            vacancy_id=vacancy.id, resume_file="fixture.pdf", cover_letter=letter,
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.add(CoverLetter(vacancy_id=vacancy.id, text=letter))
        db.commit()

    async def reject_claims(*_args, **_kwargs):
        raise workflow.CoverLetterValidationError("unsupported candidate facts")

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", reject_claims)
    async def valid_fresh_letter(*_args, **_kwargs):
        return "A fresh synthetic letter without factual claims"

    monkeypatch.setattr(workflow, "write_cover_letter", valid_fresh_letter)
    async def apply_all(*_args, **_kwargs):
        return evaluation("apply")

    sessions, session_id = await run_workflow(runtime, monkeypatch, adapter, apply_all)

    with sessions() as db:
        rows = {row.external_id: row for row in db.scalars(select(Vacancy).where(
            Vacancy.session_id == session_id,
        ))}
        assert rows["cached-invalid"].state == "ERROR"
        assert rows["cached-invalid"].data["error_code"] == "VACANCY_PROCESSING_FAILED"
        assert workflow._LETTER_CLAIMS_PROOF_KEY not in rows["cached-invalid"].data
        assert rows["healthy-after-invalid"].state == "SUBMITTED"
        assert adapter.application_opens == ["healthy-after-invalid"]
        assert adapter.submitted_ids == ["healthy-after-invalid"]
        assert db.scalar(select(Application).where(
            Application.vacancy_id == rows["cached-invalid"].id,
        )) is None


@pytest.mark.asyncio
async def test_matching_cached_letter_proof_skips_checker(runtime, monkeypatch):
    sessions, session_id = runtime
    letter = "Cached letter with previously validated facts"
    adapter = FakeAdapter(refs("cached-proven"))
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy = Vacancy(
            session_id=session_id, source="fake", external_id="cached-proven",
            url="https://fake/cached-proven", title="Cached proven", state="EVALUATING",
            data={
                workflow._COVER_LETTER_HASH_KEY: snapshot.content_hash,
                workflow._LETTER_CLAIMS_PROOF_KEY: workflow._letter_claims_proof(
                    letter, [], snapshot.content_hash,
                ),
            },
        )
        db.add(vacancy)
        db.flush()
        plan = ApplicationPlan(
            vacancy_id=vacancy.id, resume_file="fixture.pdf", cover_letter=letter,
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.add(CoverLetter(vacancy_id=vacancy.id, text=letter))
        db.commit()

    async def unexpected_checker(*_args, **_kwargs):
        pytest.fail("matching durable proof should skip repeated validation")

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", unexpected_checker)
    async def apply_all(*_args, **_kwargs):
        return evaluation("apply")

    await run_workflow(runtime, monkeypatch, adapter, apply_all)
    with sessions() as db:
        row = db.scalar(select(Vacancy).where(Vacancy.session_id == session_id))
        assert row.state == "SUBMITTED"


@pytest.mark.asyncio
async def test_cached_letter_claim_timeout_recovers_then_submits_once(runtime, monkeypatch):
    sessions, session_id = runtime
    letter = "Cached synthetic letter awaiting validation"
    adapter = FakeAdapter(refs("cached-timeout"))
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy = Vacancy(
            session_id=session_id, source="fake", external_id="cached-timeout",
            url="https://fake/cached-timeout", title="Cached timeout", state="EVALUATING",
            data={workflow._COVER_LETTER_HASH_KEY: snapshot.content_hash},
        )
        db.add(vacancy)
        db.flush()
        plan = ApplicationPlan(
            vacancy_id=vacancy.id, resume_file="fixture.pdf", cover_letter=letter,
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.add(CoverLetter(vacancy_id=vacancy.id, text=letter))
        db.commit()
    calls = 0

    async def timeout_then_valid(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise workflow.ModelTimeout("temporary claim validation timeout")

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", timeout_then_valid)
    async def apply_all(*_args, **_kwargs):
        return evaluation("apply")

    await run_workflow(runtime, monkeypatch, adapter, apply_all)
    with sessions() as db:
        row = db.scalar(select(Vacancy).where(Vacancy.session_id == session_id))
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "model_stage_retry",
        )))
        submits = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "submission",
        )))
        assert row.state == "SUBMITTED"
        assert row.data[workflow._LETTER_CLAIMS_PROOF_KEY]["version"] == workflow.LETTER_CLAIMS_VERSION
        assert len(retries) == 1 and retries[0].data["stage"] == "letter"
        assert len(submits) == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_invalid_partial_letter_preserves_cv_without_resume_or_attempt_charge(runtime, monkeypatch):
    sessions, session_id = runtime
    external_id = "partial-invalid-facts"

    class PartialAdapter(FakeAdapter):
        site_id = "hh"

        def __init__(self):
            super().__init__(refs(external_id))
            self.verify_calls = 0
            self.resume_calls = 0
            self.progress = {
                "cv_confirmed": True, "cover_letter_pending": True,
                "cover_letter_confirmed": False,
            }

        async def verify_cv_submission(self, page):
            self.verify_calls += 1
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return dict(self.progress)

    adapter = PartialAdapter()
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.application_limit = None
        item.recovery = {"search_filters": {}}
        snapshot_data = _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            }, adapter_id="hh", source_url="https://hh.ru/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot_data)
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        progress = dict(adapter.progress)
        vacancy = Vacancy(
            session_id=session_id, source="hh", external_id=external_id,
            url=f"https://hh.ru/vacancy/{external_id}", title="Partial", state="PARTIAL",
            data={
                "source": "hh", "external_id": external_id,
                "url": f"https://hh.ru/vacancy/{external_id}", "title": "Partial",
                "description": "Description", "responsibilities": [],
                "required_skills": [], "optional_skills": [],
                "submission_progress": progress, "partial_letter_attempts": 0,
            },
        )
        db.add(vacancy)
        db.flush()
        plan = ApplicationPlan(
            vacancy_id=vacancy.id, resume_file="fixture.pdf",
            cover_letter="Unsupported facts in partial letter",
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.commit()

    async def reject_claims(*_args, **_kwargs):
        raise workflow.CoverLetterValidationError("unsupported candidate facts")

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", reject_claims)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.scalar(select(Vacancy).where(
            Vacancy.session_id == session_id, Vacancy.external_id == external_id,
        ))
        stored_plan = db.scalar(select(ApplicationPlanRecord).where(
            ApplicationPlanRecord.vacancy_id == vacancy.id,
        ))
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["submission_progress"] == progress
        assert vacancy.data["partial_letter_attempts"] == 0
        assert vacancy.data["error_code"] == "VACANCY_PROCESSING_FAILED"
        assert workflow._LETTER_CLAIMS_PROOF_KEY not in vacancy.data
        assert stored_plan.data == plan
        assert adapter.verify_calls == 1
        assert adapter.resume_calls == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [workflow.ModelTimeout, workflow.ModelUnavailable])
async def test_partial_letter_checker_exhaustion_blocks_resume_and_continues_other_job(
    runtime, monkeypatch, error_type,
):
    sessions, session_id = runtime
    external_id = "partial-checker-outage"

    class PartialAdapter(FakeAdapter):
        site_id = "hh"

        def __init__(self):
            super().__init__(refs(external_id, "healthy-after-partial"))
            self.verify_calls = 0
            self.resume_calls = 0

        async def verify_cv_submission(self, page):
            self.verify_calls += 1
            return SubmissionResult(status="submitted", message="CV confirmed")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = PartialAdapter()
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.application_limit = None
        item.recovery = {"search_filters": {}}
        snapshot_data = _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            }, adapter_id="hh", source_url="https://hh.ru/resume/fixture",
        )
        persist_session_snapshot(db, session_id, snapshot_data)
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy = Vacancy(
            session_id=session_id, source="hh", external_id=external_id,
            url=f"https://hh.ru/vacancy/{external_id}", title="Partial", state="PARTIAL",
            data={
                "source": "hh", "external_id": external_id,
                "url": f"https://hh.ru/vacancy/{external_id}", "title": "Partial",
                "description": "Description", "responsibilities": [],
                "required_skills": [], "optional_skills": [],
                "submission_progress": {
                    "cv_confirmed": True, "cover_letter_pending": True,
                    "cover_letter_confirmed": False,
                },
                "partial_letter_attempts": 0,
            },
        )
        db.add(vacancy)
        db.flush()
        plan = ApplicationPlan(
            vacancy_id=vacancy.id, resume_file="fixture.pdf",
            cover_letter="Cached letter waiting for retryable review",
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.commit()

    checker_calls = 0
    completed_ids = []

    async def outage(*_args, **_kwargs):
        nonlocal checker_calls
        checker_calls += 1
        raise error_type("claim checker is unavailable")

    async def complete(_adapter, _page, plan, *_args, **_kwargs):
        completed_ids.append(plan.vacancy_id)
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="healthy submitted"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", outage)
    monkeypatch.setattr(workflow, "complete_application", complete)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)

    async def apply_all(*_args, **_kwargs):
        return evaluation("apply")

    async def write_healthy_letter(*_args, **_kwargs):
        return "Fresh healthy letter"

    monkeypatch.setattr(workflow, "evaluate", apply_all)
    monkeypatch.setattr(workflow, "write_cover_letter", write_healthy_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=10)

    with sessions() as db:
        rows = {row.external_id: row for row in db.scalars(select(Vacancy).where(
            Vacancy.session_id == session_id,
        ))}
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "model_stage_retry",
        )))
        assert rows[external_id].state == "PARTIAL"
        assert rows[external_id].data["partial_recovery_blocked"] is True
        assert rows[external_id].data["partial_letter_attempts"] == 0
        assert rows[external_id].data["submission_progress"]["cv_confirmed"] is True
        assert rows["healthy-after-partial"].state == "SUBMITTED"
        assert len(retries) == workflow._MODEL_STAGE_RETRY_LIMIT - 1
        assert adapter.resume_calls == 0
        assert completed_ids == [rows["healthy-after-partial"].id]
    assert checker_calls == workflow._MODEL_STAGE_RETRY_LIMIT


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


def _seed_hh_submitting_letter(
    sessions, session_id, *, external_id="oneclick-letter",
    letter="Durable application letter", plan_mode="valid",
):
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.status = SessionStatus.RUNNING
        item.application_limit = 1
        item.recovery = {"search_filters": {"portfolio_queries": []}}
        item.counters = {"viewed": 1, "matched": 1, "submitted": 0, "partial": 0, "errors": 0}
        persist_session_snapshot(db, session_id, _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            }, adapter_id="hh", source_url="https://hh.ru/resume/fixture",
        ))
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        vacancy = Vacancy(
            session_id=session_id, source="hh", external_id=external_id,
            url=f"https://hh.ru/vacancy/{external_id}", title="One click letter",
            state="SUBMITTING",
            data={
                "source": "hh", "external_id": external_id,
                "url": f"https://hh.ru/vacancy/{external_id}", "title": "One click letter",
                "description": "Synthetic vacancy description", "responsibilities": [],
                "required_skills": [], "optional_skills": [],
                "submission_was_absent": True,
            },
        )
        db.add(vacancy)
        db.flush()
        if plan_mode != "missing":
            plan = ApplicationPlan(
                vacancy_id=vacancy.id, resume_file="fixture.pdf", cover_letter=letter,
            ).model_dump()
            plan[workflow._RESUME_HASH_KEY] = (
                "different-resume" if plan_mode == "mismatch" else snapshot.content_hash
            )
            db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan))
        db.commit()
        return vacancy.id


@pytest.mark.asyncio
async def test_hh_oneclick_cv_reconciliation_blocks_unconfirmed_letter(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = _seed_hh_submitting_letter(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.resume_calls = 0
            self.cv_upload_calls = 0
            self.reconcile_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="CV accepted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            assert letter_expected is True
            self.reconcile_calls += 1
            return {
                "cv_confirmed": True, "cover_letter_pending": False,
                "cover_letter_confirmed": False, "letter_recovery_safe": False,
            }

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["partial_recovery_blocked"] is True
        assert vacancy.data["submission_progress"] == {
            "cv_confirmed": True,
            "cover_letter_pending": True,
            "cover_letter_confirmed": False,
        }
        assert vacancy.data["error_code"] == "SUBMISSION_UNCONFIRMED"
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == adapter.cv_upload_calls == 0


@pytest.mark.asyncio
async def test_hh_missing_letter_reconciler_keeps_verified_cv_as_blocked_partial(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = _seed_hh_submitting_letter(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None
        reconcile_submission_progress = None

        def __init__(self):
            super().__init__([])
            self.resume_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="CV accepted")

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["partial_recovery_blocked"] is True
        assert vacancy.data["submission_progress"] == {
            "cv_confirmed": True,
            "cover_letter_pending": True,
            "cover_letter_confirmed": False,
        }
        assert vacancy.data["partial_counted"] is True
        assert item.counters["partial"] == 1
        assert item.counters["submitted"] == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is None
    assert adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_hh_letter_safe_reconciliation_resumes_only_letter_once(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = _seed_hh_submitting_letter(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.progress = None
            self.resume_calls = 0
            self.cv_upload_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="CV accepted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            assert letter_expected is True
            return {
                "cv_confirmed": True, "cover_letter_pending": True,
                "cover_letter_confirmed": False, "letter_recovery_safe": True,
            }

        async def verify_cv_submission(self, page):
            return SubmissionResult(status="submitted", message="CV accepted")

        async def resume_application(self, page, plan, *, cv_confirmed, cover_letter_pending):
            assert cv_confirmed and cover_letter_pending
            with sessions() as db:
                durable = db.get(Vacancy, vacancy_id)
                assert durable.state == "PARTIAL"
                assert durable.data["submission_progress"]["cv_confirmed"] is True
            self.resume_calls += 1
            return ApplicationForm()

        def get_submission_progress(self):
            return self.progress

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)

    async def accept_letter_claims(*_args, **_kwargs):
        return None

    async def finish_letter(_adapter, _page, _plan, *_args, **kwargs):
        adapter.progress = {
            "cv_confirmed": True, "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="Letter confirmed"),
            error_code=None, error_message=None, stopped=False,
        )

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", accept_letter_claims)
    monkeypatch.setattr(workflow, "complete_application", finish_letter)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "SUBMITTED"
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is not None
        assert vacancy.data["submission_progress"]["cover_letter_confirmed"] is True
    assert adapter.resume_calls == 1
    assert adapter.cv_upload_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_mode", ["missing", "mismatch"])
async def test_hh_reconciliation_requires_plan_for_oneclick_letter_and_keeps_no_letter_legacy_full(
    runtime, monkeypatch, plan_mode,
):
    sessions, session_id = runtime
    missing_id = _seed_hh_submitting_letter(sessions, session_id, plan_mode=plan_mode)

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="Previously submitted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            self.reconcile_calls += 1
            return {
                "cv_confirmed": True, "cover_letter_pending": False,
                "cover_letter_confirmed": False, "letter_recovery_safe": False,
            }

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        missing = db.get(Vacancy, missing_id)
        assert missing.state == "PARTIAL"
        assert missing.data["partial_recovery_blocked"] is True
        assert missing.data["submission_progress"]["cv_confirmed"] is True
        assert missing.data.get("partial_counted") is True
        assert db.scalar(select(Application).where(Application.vacancy_id == missing_id)) is None
    assert adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_hh_reconciliation_without_desired_letter_keeps_legacy_full_result(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = _seed_hh_submitting_letter(sessions, session_id, letter="")

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="Previously submitted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            self.reconcile_calls += 1
            raise AssertionError("no-letter submissions need no letter reconciliation")

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.get(Vacancy, vacancy_id)
        assert vacancy.state == "SUBMITTED"
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is not None
    assert adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_hh_reconciliation_with_confirmed_letter_records_full_without_resume(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = _seed_hh_submitting_letter(sessions, session_id)

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.reconcile_calls = 0
            self.resume_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="Previously submitted")

        async def reconcile_submission_progress(self, page, *, letter_expected):
            assert letter_expected is True
            self.reconcile_calls += 1
            return {
                "cv_confirmed": True, "cover_letter_pending": False,
                "cover_letter_confirmed": True, "letter_recovery_safe": False,
            }

        async def resume_application(self, *args, **kwargs):
            self.resume_calls += 1
            return ApplicationForm()

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        vacancy = db.get(Vacancy, vacancy_id)
        item = db.get(JobSession, session_id)
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["submission_progress"]["cover_letter_confirmed"] is True
        assert item.counters["submitted"] == 1
        assert db.scalar(select(Application).where(Application.vacancy_id == vacancy_id)) is not None
    assert adapter.reconcile_calls == 1
    assert adapter.resume_calls == 0


@pytest.mark.asyncio
async def test_hh_durable_vacancy_letter_confirmation_skips_adapter_reconciliation(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = _seed_hh_submitting_letter(sessions, session_id)
    with sessions() as db:
        vacancy = db.get(Vacancy, vacancy_id)
        vacancy.data = {
            **vacancy.data,
            "submission_progress": {
                "cv_confirmed": True,
                "cover_letter_pending": False,
                "cover_letter_confirmed": True,
            },
        }
        db.commit()

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__([])
            self.cv_calls = 0

        async def verify_submission(self, page):
            return SubmissionResult(status="already_applied", message="Previously submitted")

        async def reconcile_submission_progress(self, page, *, letter_expected: bool):
            raise AssertionError("durable vacancy letter confirmation should avoid adapter reconciliation")

        async def verify_cv_submission(self, page):
            self.cv_calls += 1
            raise AssertionError("confirmed full submission should not need a CV-only recovery")

        async def resume_application(self, *args, **kwargs):
            raise AssertionError("confirmed full submission should not resume the letter")

    adapter = Adapter()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.get(Vacancy, vacancy_id)
        applications = list(db.scalars(select(Application).where(
            Application.vacancy_id == vacancy_id,
        )))
        assert vacancy.state == "SUBMITTED"
        assert vacancy.data["submission_progress"]["cover_letter_confirmed"] is True
        assert item.counters["submitted"] == 1
        assert len(applications) == 1
    assert adapter.cv_calls == 0


@pytest.mark.asyncio
async def test_hh_open_application_checkpoints_progress_before_captcha(runtime, monkeypatch):
    sessions, session_id = runtime

    class Adapter(FakeAdapter):
        site_id = "hh"
        collect_more_job_refs = None

        def __init__(self):
            super().__init__(refs("captcha-before-letter"))
            self.progress = None

        async def open_application(self, page):
            self.progress = {
                "cv_confirmed": True, "cover_letter_pending": True,
                "cover_letter_confirmed": False,
            }
            raise workflow.CaptchaRequired("CAPTCHA after one-click CV")

        def get_submission_progress(self):
            return self.progress

    adapter = Adapter()
    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.adapter_id = "hh"
        item.application_limit = 1
        item.recovery = {"search_filters": {"portfolio_queries": []}}
        persist_session_snapshot(db, session_id, _normalize_extracted(
            {
                "external_id": "fixture", "identity": {"full_name": "Test", "gender": "male"},
                "target": {"title": "Role"}, "about": "Fixture professional background",
                "skills": [{"name": "Python"}],
            }, adapter_id="hh", source_url="https://hh.ru/resume/fixture",
        ))
        db.commit()

    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy).where(Vacancy.session_id == session_id))
        assert item.status == SessionStatus.PAUSED
        assert vacancy.state == "PARTIAL"
        assert vacancy.data["submission_progress"] == adapter.progress
        assert item.counters["submitted"] == 0


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
        assert db.scalar(select(Application).where(Application.vacancy_id == rows["stuck"].id)) is None
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
            assert application is None
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
    browser_closes = []

    async def record_browser_close(session_id):
        browser_closes.append(session_id)

    monkeypatch.setattr(workflow, "close_browser", record_browser_close)

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
        assert db.scalar(select(Vacancy)).state != "ERROR"
        assert any(event.event_type == "human_required" for event in db.scalars(select(BrowserEvent)))
    assert browser_closes == []


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
async def test_model_recovery_is_bounded_but_authentication_wait_is_steady(runtime, error_type):
    manager = workflow.WorkflowManager()
    manager.retry_base_seconds = 0.001
    manager.retry_max_seconds = 10
    with runtime[0]() as db:
        item = db.get(JobSession, runtime[1])
        item.status = SessionStatus.RUNNING
        item.recovery = {"attempt": (
            0 if error_type is workflow.ModelUnavailable
            else workflow._SESSION_RECOVERY_RETRY_LIMIT
        )}
        db.commit()

    outcomes = []
    for _ in range(workflow._SESSION_RECOVERY_RETRY_LIMIT + 3):
        outcomes.append(await manager._recover(runtime[1], error_type("dependency unavailable")))

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
        if error_type is workflow.ModelUnavailable:
            assert outcomes.count(True) == workflow._SESSION_RECOVERY_RETRY_LIMIT
            assert outcomes[-1] is False
            assert item.status == SessionStatus.FAILED
        else:
            assert all(outcomes)
            assert item.status == SessionStatus.RUNNING
        delays = [event.data["delay_seconds"] for event in retries]
        if error_type is workflow.ModelUnavailable:
            assert delays == [
                min(manager.retry_max_seconds, manager.retry_base_seconds * 2 ** min(index, 10))
                for index in range(len(delays))
            ]
            assert len(delays) == workflow._SESSION_RECOVERY_RETRY_LIMIT
        else:
            assert all(delay == manager.retry_base_seconds for delay in delays)
        assert bool(failures) is (error_type is workflow.ModelUnavailable)


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
@pytest.mark.parametrize("error_type", [workflow.ModelUnavailable, workflow.ModelTimeout])
async def test_model_outage_retries_saved_vacancy_without_reopening_search(
    runtime, monkeypatch, error_type,
):
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
            raise error_type("temporary outage")
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
        model_metrics = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "metric_model",
        ).order_by(BrowserEvent.id)))
        submissions = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "submission",
        ).order_by(BrowserEvent.id)))
        assert vacancies["recover-in-place"].state == "SUBMITTED"
        assert vacancies["healthy"].state == "SUBMITTED"
        assert vacancies["recover-in-place"].data.get("model_retry_budgets") is None
        assert len(retries) == 1
        assert retries[0].data["stage"] == "evaluation"
        assert retries[0].data["error_type"] == error_type.__name__
        assert model_metrics[0].data["outcome"] == (
            "logical_timeout" if error_type is workflow.ModelTimeout else "unavailable"
        )
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
async def test_letter_model_timeout_retries_saved_vacancy(runtime, monkeypatch):
    letter_calls = 0

    async def apply_all(*_args, **_kwargs):
        return evaluation("apply")

    async def write_letter(*_args, **_kwargs):
        nonlocal letter_calls
        letter_calls += 1
        if letter_calls == 1:
            raise workflow.ModelTimeout("logical request timeout")
        return "Synthetic cover letter for this vacancy"

    with runtime[0]() as db:
        db.get(JobSession, runtime[1]).guaranteed_application = True
        db.commit()
    adapter = FakeAdapter(refs("letter-timeout"))
    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow, "write_cover_letter", write_letter)
    monkeypatch.setattr(workflow, "evaluate", apply_all)
    sessions, session_id = runtime
    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=5)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = db.scalar(select(Vacancy).where(Vacancy.session_id == session_id))
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "model_stage_retry",
        )))
        model_metrics = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "metric_model",
        ).order_by(BrowserEvent.id)))
        assert vacancy.state == "SUBMITTED"
        assert item.status == SessionStatus.COMPLETED
        assert item.counters["errors"] == 0
        assert item.counters["matched"] == item.counters["submitted"] == 1
        assert vacancy.data.get("model_retry_budgets") is None
        assert len(retries) == 1
        assert retries[0].data == {
            "vacancy_id": vacancy.id,
            "stage": "letter",
            "attempt": 1,
            "delay_seconds": 0,
            "error_type": "ModelTimeout",
        }
        letter_metric = next(
            metric for metric in model_metrics if metric.data.get("stage") == "letter"
        )
        assert letter_metric.data["outcome"] == "logical_timeout"
    assert letter_calls == 2


@pytest.mark.asyncio
async def test_model_timeout_stage_budget_exhaustion_marks_error_without_deferral(runtime):
    sessions, session_id = runtime
    with sessions() as db:
        item = db.get(JobSession, session_id)
        vacancy = Vacancy(
            session_id=session_id,
            source="fake",
            external_id="exhausted-timeout",
            url="https://fake/exhausted-timeout",
            title="Exhausted timeout",
            state="EVALUATING",
            data={"model_retry_budgets": {"evaluation": workflow._MODEL_STAGE_RETRY_LIMIT - 1}},
        )
        db.add(vacancy)
        db.flush()
        calls = 0

        async def timeout():
            nonlocal calls
            calls += 1
            raise workflow.ModelTimeout("logical timeout")

        succeeded, result = await workflow.WorkflowManager()._model_stage_call(
            db, item, vacancy, "evaluation", timeout,
        )
        db.refresh(vacancy)
        events = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type.in_(("model_stage_retry", "vacancy_error")),
        )))
        assert (succeeded, result) == (False, None)
        assert calls == 1
        assert vacancy.state == "ERROR"
        assert vacancy.data["error_code"] == "VACANCY_PROCESSING_FAILED"
        assert vacancy.data["model_retry_budgets"]["evaluation"] == workflow._MODEL_STAGE_RETRY_LIMIT
        assert "model_retry_stage" not in vacancy.data
        assert len(events) == 1
        assert events[0].event_type == "vacancy_error"


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
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "model_stage_retry",
        )))
        assert vacancies["permanent"].state == "ERROR"
        assert vacancies["healthy"].state == "SUBMITTED"
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
        assert retries == []
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
        contract_version=workflow.POLICY_CONTRACT_VERSION,
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

    async def current_policy_must_be_reused(*_args, **_kwargs):
        pytest.fail("a current version-2 policy should be reused")

    monkeypatch.setattr(workflow, "compile_preference_policy", current_policy_must_be_reused)
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
async def test_legacy_preference_policy_is_recompiled_before_analysis(runtime, monkeypatch):
    sessions, session_id = runtime
    adapter = FakeAdapter(refs("legacy-policy"))
    old_policy = DesiredJobPolicy(
        contract_version=1,
        green_flags=[PreferenceFlag(id="green-1", text="Python", category="other")],
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.desired_job_description = "Senior Python без ГПХ"
        item.preference_policy = old_policy.model_dump(mode="json")
        db.commit()

    order = []
    compiled = DesiredJobPolicy(
        contract_version=workflow.POLICY_CONTRACT_VERSION,
        red_flags=[PreferenceFlag(id="red-1", text="ГПХ", category="other")],
    )

    async def compile_current(_gateway, description):
        order.append("compile")
        assert description == "Senior Python без ГПХ"
        return compiled

    async def evaluate_with_compiled_policy(*args, **kwargs):
        order.append("evaluate")
        policy = args[-1] if len(args) >= 6 else kwargs.get("preference_policy")
        assert policy is not None
        assert policy.contract_version == workflow.POLICY_CONTRACT_VERSION
        assert [flag.text for flag in policy.red_flags] == ["ГПХ"]
        return evaluation("skip")

    monkeypatch.setattr(workflow, "compile_preference_policy", compile_current)
    await run_workflow(runtime, monkeypatch, adapter, evaluate_with_compiled_policy)

    with sessions() as db:
        stored = db.get(JobSession, session_id).preference_policy
        assert stored["contract_version"] == workflow.POLICY_CONTRACT_VERSION
        assert stored["red_flags"][0]["text"] == "ГПХ"
    assert order == ["compile", "evaluate"]


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
@pytest.mark.parametrize("error_type", [workflow.ModelUnavailable, workflow.ModelTimeout])
async def test_preference_compilation_outage_is_retried_inside_workflow(
    runtime, monkeypatch, error_type,
):
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
            raise error_type("offline")
        return DesiredJobPolicy(contract_version=workflow.POLICY_CONTRACT_VERSION)

    monkeypatch.setattr(workflow, "compile_preference_policy", compile_policy)
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=5)
    with runtime[0]() as db:
        assert db.get(JobSession, runtime[1]).preference_policy is not None
        assert db.get(JobSession, runtime[1]).counters["submitted"] == 1
        retries = list(db.scalars(select(BrowserEvent).where(
            BrowserEvent.session_id == runtime[1],
            BrowserEvent.event_type == "recovery_retry",
        )))
        assert len(retries) == 1
        assert retries[0].data["error_type"] == error_type.__name__
    assert calls == 2
