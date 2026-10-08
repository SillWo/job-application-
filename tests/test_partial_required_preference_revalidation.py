from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_workflow_zarplata_letter_recovery import (
    FakeAdapter,
    partial_progress,
    seed_zarplata_session,
)

from backend.adapters.base.protocol import SubmissionResult
from backend.intelligence.gateway import ModelPermanentError, ModelTimeout
from backend.intelligence.preference_policy import POLICY_CONTRACT_VERSION
from backend.intelligence.security import PromptInjectionDetected
from backend.orchestrator import workflow
from backend.persistence.models import (
    Application,
    ApplicationPlanRecord,
    Evaluation,
    JobSession,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.schemas.domain import ApplicationPlan, DesiredJobPolicy, JobEvaluation, PreferenceFlag

pytest_plugins = "test_workflow_zarplata_letter_recovery"


class Gateway:
    provider = "mock"

    def set_context(self, **_kwargs):
        return None


class PartialAdapter(FakeAdapter):
    site_id = "zarplata"
    collect_more_job_refs = None

    def __init__(self):
        super().__init__([])
        self.progress = partial_progress()
        self.resume_calls = 0
        self.cv_verification_calls = 0

    async def verify_cv_submission(self, _page):
        self.cv_verification_calls += 1
        return SubmissionResult(status="submitted", message="CV confirmed")

    async def resume_application(self, _page, _plan, *, cv_confirmed, cover_letter_pending):
        assert cv_confirmed and cover_letter_pending
        self.resume_calls += 1
        return None

    def get_submission_progress(self):
        return self.progress


def policy(*, required=True):
    return DesiredJobPolicy(
        contract_version=POLICY_CONTRACT_VERSION,
        green_flags=[PreferenceFlag(
            id="required-role", text="Synthetic required role", category="desired_task",
            required=required, source_quote="Synthetic posting description",
        )],
    )


def prepare_recovery(sessions, session_id, *, evaluation_data="missing", decision="apply"):
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL", progress=partial_progress(),
    )
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.desired_job_description = "Synthetic required role"
        item.preference_policy = policy().model_dump(mode="json")
        vacancy = db.get(Vacancy, vacancy_id)
        vacancy.data = {
            **vacancy.data,
            "description": "Synthetic posting description",
            "evaluation_security_version": workflow._EVALUATION_SECURITY_VERSION,
        }
        if evaluation_data != "missing":
            cached = JobEvaluation(
                decision=decision,
                score=90 if decision == "apply" else 10,
                confidence=0.9,
                category="synthetic",
                reason="fixture",
            ).model_dump()
            if evaluation_data == "compatible":
                cached[workflow._EVALUATION_FINGERPRINT_KEY] = "current-fingerprint"
            elif evaluation_data == "stale":
                cached[workflow._EVALUATION_FINGERPRINT_KEY] = "stale-fingerprint"
            db.add(Evaluation(vacancy_id=vacancy_id, data=cached))
        db.commit()
    return vacancy_id


def configure(monkeypatch, adapter, *, fingerprint="current-fingerprint"):
    gateway = Gateway()
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    monkeypatch.setattr(workflow, "ModelGateway", lambda: gateway)
    monkeypatch.setattr(workflow, "_evaluation_fingerprint", lambda *_args, **_kwargs: fingerprint)

    async def accept_letter_claims(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow, "validate_existing_letter_claims", accept_letter_claims)
    calls = {"letter": 0}

    async def complete(*_args, **_kwargs):
        calls["letter"] += 1
        adapter.progress = {
            "cv_confirmed": True,
            "cover_letter_pending": False,
            "cover_letter_confirmed": True,
        }
        return SimpleNamespace(
            submission=SubmissionResult(status="submitted", message="Letter confirmed"),
            error_code=None,
            error_message=None,
            stopped=False,
        )

    monkeypatch.setattr(workflow, "complete_application", complete)
    return calls


def patch_evaluator(monkeypatch, implementation):
    async def evaluate(*_args, **_kwargs):
        return await implementation(*_args, **_kwargs)

    monkeypatch.setattr(workflow, "evaluate", evaluate)


async def run(runtime):
    await asyncio.wait_for(workflow.WorkflowManager().run(runtime[1]), timeout=10)


def read_vacancy(sessions, vacancy_id):
    with sessions() as db:
        vacancy = db.get(Vacancy, vacancy_id)
        item = db.get(JobSession, vacancy.session_id)
        return vacancy.state, dict(vacancy.data), dict(item.counters or {}), db.scalar(
            select(Application).where(Application.vacancy_id == vacancy_id)
        )


@pytest.mark.asyncio
async def test_stale_required_evaluation_skip_preserves_cv_and_never_resumes_letter(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="stale")
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)
    evaluations = 0

    async def skip_evaluation(*_args, **_kwargs):
        nonlocal evaluations
        evaluations += 1
        return JobEvaluation(decision="skip", score=20, confidence=0.9, category="synthetic", reason="required unmet")

    patch_evaluator(monkeypatch, skip_evaluation)
    await run(runtime)

    state, data, counters, application = read_vacancy(sessions, vacancy_id)
    assert evaluations == 1
    assert state == "PARTIAL"
    assert data["partial_recovery_blocked"] is True
    assert data["submission_progress"]["cv_confirmed"] is True
    assert data["submission_progress"]["cover_letter_pending"] is True
    assert adapter.resume_calls == calls["letter"] == 0
    assert application is None
    assert counters["partial"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation_data", ["stale", "missing"])
async def test_stale_or_missing_required_evaluation_rechecks_then_resumes_only_letter(
    runtime, monkeypatch, evaluation_data,
):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data=evaluation_data)
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)
    evaluations = 0

    async def apply_evaluation(*_args, **_kwargs):
        nonlocal evaluations
        evaluations += 1
        return JobEvaluation(decision="apply", score=90, confidence=0.9, category="synthetic", reason="required confirmed")

    patch_evaluator(monkeypatch, apply_evaluation)
    await run(runtime)

    state, data, counters, application = read_vacancy(sessions, vacancy_id)
    assert evaluations == 1
    assert adapter.cv_verification_calls == 1
    assert adapter.resume_calls == calls["letter"] == 1
    assert state == "SUBMITTED"
    assert data["submission_progress"]["cover_letter_confirmed"] is True
    assert counters["submitted"] == 1
    assert application is not None


@pytest.mark.asyncio
async def test_compatible_apply_uses_saved_evaluation_without_new_model_evaluation(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="compatible")
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)

    async def unexpected_evaluation(*_args, **_kwargs):
        raise AssertionError("compatible approved evaluation should be reused")

    patch_evaluator(monkeypatch, unexpected_evaluation)
    await run(runtime)

    state, _data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert adapter.resume_calls == calls["letter"] == 1
    assert state == "SUBMITTED"
    assert application is not None


@pytest.mark.asyncio
async def test_compatible_skip_blocks_letter_recovery(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="compatible", decision="skip")
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)

    async def unexpected_evaluation(*_args, **_kwargs):
        raise AssertionError("compatible skip must remain blocked")

    patch_evaluator(monkeypatch, unexpected_evaluation)
    await run(runtime)

    state, data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert state == "PARTIAL"
    assert data["partial_recovery_blocked"] is True
    assert adapter.resume_calls == calls["letter"] == 0
    assert application is None


@pytest.mark.asyncio
async def test_required_verification_timeout_defers_then_exhausts_without_outgoing(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="stale")
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)
    attempts = 0

    async def timeout_evaluation(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ModelTimeout("synthetic timeout")

    patch_evaluator(monkeypatch, timeout_evaluation)
    await run(runtime)

    state, data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert attempts == workflow._MODEL_STAGE_RETRY_LIMIT
    assert state == "PARTIAL"
    assert data["partial_recovery_blocked"] is True
    assert data["submission_progress"]["cv_confirmed"] is True
    assert adapter.resume_calls == calls["letter"] == 0
    assert application is None


@pytest.mark.asyncio
async def test_required_verification_permanent_error_blocks_only_partial_vacancy(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="stale")
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == session_id,
        ))
        other = Vacancy(
            session_id=session_id, source="zarplata", external_id="healthy",
            url="https://zarplata.ru/vacancy/healthy", title="Healthy partial",
            state="PARTIAL",
            data={
                "source": "zarplata", "external_id": "healthy",
                "url": "https://zarplata.ru/vacancy/healthy", "title": "Healthy partial",
                "description": "Synthetic posting description", "responsibilities": [],
                "required_skills": [], "optional_skills": [],
                "submission_progress": partial_progress(), "partial_counted": True,
                "partial_letter_attempts": 0,
            },
        )
        db.add(other)
        db.flush()
        plan = ApplicationPlan(
            vacancy_id=other.id, resume_file="fixture.pdf",
            cover_letter="Supported synthetic candidate letter",
        ).model_dump()
        plan[workflow._RESUME_HASH_KEY] = snapshot.content_hash
        db.add(ApplicationPlanRecord(vacancy_id=other.id, data=plan))
        other_id = other.id
        db.commit()
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)

    async def permanent_error(posting, *_args, **_kwargs):
        if posting.external_id == "z-letter-recovery":
            raise ModelPermanentError("provider refused")
        return JobEvaluation(
            decision="apply", score=90, confidence=0.9,
            category="synthetic", reason="unrelated fixture vacancy has an independent approval",
        )

    patch_evaluator(monkeypatch, permanent_error)
    await run(runtime)

    state, data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert state == "PARTIAL"
    assert data["partial_recovery_blocked"] is True
    assert data["error_code"] == "VACANCY_PROCESSING_FAILED"
    assert adapter.resume_calls == calls["letter"] == 1
    assert application is None
    with sessions() as db:
        healthy = db.get(Vacancy, other_id)
        assert healthy.state == "SUBMITTED"
        assert db.scalar(select(Application).where(Application.vacancy_id == other_id)) is not None


@pytest.mark.asyncio
async def test_required_verification_security_error_blocks_partial_without_outgoing(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="stale")
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)

    async def unsafe_evaluation(*_args, **_kwargs):
        raise PromptInjectionDetected("synthetic unsafe model output")

    patch_evaluator(monkeypatch, unsafe_evaluation)
    await run(runtime)

    state, data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert state == "PARTIAL"
    assert data["partial_recovery_blocked"] is True
    assert data["security_incident_recorded"] is True
    assert data["submission_progress"]["cv_confirmed"] is True
    assert adapter.resume_calls == calls["letter"] == 0
    assert application is None


@pytest.mark.asyncio
async def test_no_applicable_required_flags_keep_existing_partial_resume_behavior(runtime, monkeypatch):
    sessions, session_id = runtime
    vacancy_id = prepare_recovery(sessions, session_id, evaluation_data="stale")
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.preference_policy = policy(required=False).model_dump(mode="json")
        db.commit()
    adapter = PartialAdapter()
    calls = configure(monkeypatch, adapter)

    async def unexpected_evaluation(*_args, **_kwargs):
        raise AssertionError("legacy policy must preserve existing recovery behavior")

    patch_evaluator(monkeypatch, unexpected_evaluation)
    await run(runtime)

    state, _data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert adapter.resume_calls == calls["letter"] == 1
    assert state == "SUBMITTED"
    assert application is not None


@pytest.mark.asyncio
async def test_already_attempted_letter_stays_read_only_during_partial_recovery(runtime, monkeypatch):
    sessions, session_id = runtime
    progress = {**partial_progress(), "cover_letter_attempted": True}
    _external_id, vacancy_id = seed_zarplata_session(
        sessions, session_id, with_vacancy=True, state="PARTIAL", progress=progress,
    )
    adapter = PartialAdapter()
    adapter.progress = progress
    calls = configure(monkeypatch, adapter)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        item.desired_job_description = "Synthetic required role"
        item.preference_policy = policy().model_dump(mode="json")
        db.commit()

    async def unexpected_evaluation(*_args, **_kwargs):
        raise AssertionError("attempted letter must reconcile read-only before evaluating")

    patch_evaluator(monkeypatch, unexpected_evaluation)

    async def reconciler(_page, *, letter_expected):
        assert letter_expected is True
        return partial_progress()

    adapter.reconcile_submission_progress = reconciler
    await run(runtime)

    state, data, _counters, application = read_vacancy(sessions, vacancy_id)
    assert state == "PARTIAL"
    assert data["partial_recovery_blocked"] is True
    assert data["submission_progress"]["cover_letter_attempted"] is True
    assert adapter.resume_calls == calls["letter"] == 0
    assert application is None
