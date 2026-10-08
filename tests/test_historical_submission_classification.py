"""Regressions for cross-session submission outcomes and historical aliases."""

import asyncio

import pytest
from sqlalchemy import select
from test_workflow_non_captcha_continuation import FakeAdapter, evaluation
from test_workflow_recovery import configure, refs
from test_workflow_recovery import runtime as recovery_runtime

from backend.orchestrator import workflow
from backend.persistence.models import Application, Evaluation, JobSession, Vacancy
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot
from backend.services.vacancy_history_presentation import resolve_external_history

runtime = recovery_runtime


def _session(db, *, minimum_scores=None):
    item = JobSession(
        adapter_id="fake",
        status=SessionStatus.COMPLETED,
        counters={},
        application_limit=None,
        minimum_scores=minimum_scores or {},
    )
    db.add(item)
    db.flush()
    return item


def _vacancy(db, session, external_id, state, data=None):
    item = Vacancy(
        session_id=session.id,
        source="fake",
        external_id=external_id,
        url=f"https://fake/{external_id}",
        title=external_id,
        state=state,
        data=data or {},
    )
    db.add(item)
    db.flush()
    return item


def test_resolver_classifies_original_submission_outcomes(runtime):
    sessions, _ = runtime
    with sessions() as db:
        old_session = _session(db)
        cases = {
            "attempted": ("ERROR", {"submission_attempted": True}),
            "explicit-unknown": (
                "SUBMISSION_UNCONFIRMED",
                {"error_code": "SUBMISSION_UNCONFIRMED"},
            ),
            "partial": (
                "PARTIAL",
                {"submission_progress": {"cv_confirmed": True, "cover_letter_pending": True}},
            ),
            "site-observed": ("ALREADY_APPLIED", {"site_observed": True}),
        }
        origins = {
            key: _vacancy(db, old_session, key, state, data)
            for key, (state, data) in cases.items()
        }
        confirmed = _vacancy(db, old_session, "confirmed", "SUBMITTED", {})
        db.add(Application(vacancy_id=confirmed.id, status="submitted"))

        resolutions = {
            key: resolve_external_history(db, origin)
            for key, origin in [*origins.items(), ("confirmed", confirmed)]
        }

        assert resolutions["attempted"].outcome == "unconfirmed"
        assert resolutions["explicit-unknown"].outcome == "unconfirmed"
        assert resolutions["partial"].outcome == "partial"
        assert resolutions["site-observed"].outcome == "already_applied"
        assert resolutions["confirmed"].outcome == "confirmed"
        assert all(not result.invalid_history for result in resolutions.values())


@pytest.mark.parametrize("malformation", ["missing", "mismatch", "cycle"])
def test_resolver_fails_closed_on_malformed_alias_history(runtime, malformation):
    sessions, _ = runtime
    with sessions() as db:
        origin_session = _session(db)
        alias_session = _session(db)
        _vacancy(
            db, origin_session, "same", "ERROR", {"submission_attempted": True}
        )
        if malformation == "missing":
            pointer = 999999
        elif malformation == "mismatch":
            other = _vacancy(db, origin_session, "different", "ERROR", {})
            pointer = other.id
        else:
            pointer = None
        alias = _vacancy(
            db,
            alias_session,
            "same",
            "SUBMISSION_UNCONFIRMED",
            {"cross_session_suppressed": True, "historical_vacancy_id": pointer},
        )
        if malformation == "cycle":
            cycle_session = _session(db)
            second_alias = _vacancy(
                db,
                cycle_session,
                "same",
                "SUBMISSION_UNCONFIRMED",
                {"cross_session_suppressed": True, "historical_vacancy_id": alias.id},
            )
            alias.data = {
                "cross_session_suppressed": True,
                "historical_vacancy_id": second_alias.id,
            }
            db.flush()

        resolution = resolve_external_history(db, alias)

        assert resolution.invalid_history is True
        assert resolution.outcome == "unconfirmed"
        assert resolution.origin is None


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupted_mirror", [False, True], ids=["three-session", "corrupted-mirror-chain"])
async def test_suppressed_unknown_alias_stays_unknown_and_workflow_continues(
    runtime, monkeypatch, corrupted_mirror,
):
    sessions, session_id = runtime
    unknown_id = "unknown-chain"
    healthy_id = "healthy"

    class Adapter(FakeAdapter):
        def __init__(self, job_refs):
            super().__init__(job_refs)
            self.extracted = []
            self.submitted = []

        async def extract_job(self, page):
            self.extracted.append(self.current_ref.external_id)
            return await super().extract_job(page)

        async def submit_application(self, page):
            self.submitted.append(self.current_ref.external_id)
            return await super().submit_application(page)

    adapter = Adapter(refs(unknown_id, healthy_id))
    evaluated = []

    async def evaluate_current(posting, *args, **kwargs):
        evaluated.append(posting.external_id)
        return evaluation("apply")

    configure(monkeypatch, adapter)
    monkeypatch.setattr(workflow, "evaluate", evaluate_current)

    with sessions() as db:
        source_session = _session(db, minimum_scores={"overall": 70})
        alias_session = _session(db, minimum_scores={"overall": 70})
        corrupted_mirror_session = (
            _session(db, minimum_scores={"overall": 70}) if corrupted_mirror else None
        )
        source = _vacancy(
            db,
            source_session,
            unknown_id,
            "ERROR",
            {"submission_attempted": True, "error_code": "SUBMISSION_UNCONFIRMED"},
        )
        alias = _vacancy(
            db,
            alias_session,
            unknown_id,
            "SUBMISSION_UNCONFIRMED",
            {
                "cross_session_suppressed": True,
                "historical_vacancy_id": source.id,
                "error_code": "SUBMISSION_UNCONFIRMED",
            },
        )
        if corrupted_mirror_session is not None:
            _vacancy(
                db,
                corrupted_mirror_session,
                unknown_id,
                "ALREADY_APPLIED",
                {
                    "cross_session_suppressed": True,
                    "historical_vacancy_id": alias.id,
                },
            )
        current = db.get(JobSession, session_id)
        current.adapter_id = "fake"
        current.application_limit = None
        # Changed settings cannot turn a historic unknown into a safe new send.
        current.minimum_scores = {"tasks": 4}
        current.recovery = {"search_filters": {}}
        persist_session_snapshot(
            db,
            session_id,
            _normalize_extracted(
                {
                    "external_id": "fixture",
                    "identity": {"full_name": "Fixture"},
                    "target": {"title": "Role"},
                    "about": "Synthetic candidate",
                    "skills": [{"name": "Python"}],
                },
                adapter_id="fake",
                source_url="https://fake/resume/fixture",
            ),
        )
        db.commit()

    await asyncio.wait_for(workflow.WorkflowManager().run(session_id), timeout=8)

    with sessions() as db:
        rows = {
            row.external_id: row
            for row in db.scalars(select(Vacancy).where(Vacancy.session_id == session_id))
        }
        item = db.get(JobSession, session_id)
        assert item.status == SessionStatus.COMPLETED
        assert rows[unknown_id].state == "SUBMISSION_UNCONFIRMED"
        assert rows[unknown_id].data["historical_vacancy_id"] == source.id
        assert rows[healthy_id].state == "SUBMITTED"
        assert evaluated == [healthy_id]
        assert adapter.extracted == [healthy_id]
        assert adapter.submitted == [healthy_id]
        assert item.counters["submitted"] == 1
        assert item.counters.get("already_applied", 0) == 0
        assert item.counters.get("partial", 0) == 0
        assert db.scalar(select(Application).where(Application.vacancy_id == rows[unknown_id].id)) is None
        assert db.scalar(select(Evaluation).where(Evaluation.vacancy_id == rows[unknown_id].id)) is None
