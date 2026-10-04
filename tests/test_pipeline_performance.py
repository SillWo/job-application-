from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import (
    ApplicationForm,
    FillResult,
    JobRef,
    LoginState,
    SubmissionResult,
)
from backend.orchestrator import workflow
from backend.orchestrator.hh_application import ApplicationOutcome
from backend.persistence.database import Base
from backend.persistence.models import Application, BrowserEvent, JobSession, Vacancy
from backend.schemas.domain import JobEvaluation, JobPosting, SessionStatus
from backend.services.resume_session import _normalize_extracted, persist_session_snapshot


class HHWorkflowMock:
    site_id = "hh"
    display_name = "HH mock"
    search_exhausted = True

    def __init__(self, jobs: list[JobRef], browser_delay: float) -> None:
        self.jobs = jobs
        self.browser_delay = browser_delay
        self.current_ref: JobRef | None = None
        self.submitted: list[str] = []
        self.active_browser = 0
        self.max_browser = 0

    async def _browser_step(self):
        self.active_browser += 1
        self.max_browser = max(self.max_browser, self.active_browser)
        await asyncio.sleep(self.browser_delay)
        self.active_browser -= 1

    async def get_login_state(self, page):
        return LoginState(authenticated=True, message="ok")

    async def open_search(self, page, filters):
        self.current_ref = None

    async def collect_job_refs(self, page):
        return self.jobs

    async def open_job(self, page, ref):
        await self._browser_step()
        self.current_ref = ref

    async def detect_blockers(self, page):
        return []

    async def extract_job(self, page):
        await self._browser_step()
        assert self.current_ref is not None
        ref = self.current_ref
        return JobPosting(
            source=self.site_id,
            external_id=ref.external_id,
            url=ref.url,
            title=f"Synthetic role {ref.external_id}",
            description="Python engineering position",
        )

    async def open_application(self, page):
        await self._browser_step()
        return ApplicationForm()

    async def prepare_application(self, page, plan):
        return ApplicationForm()

    async def read_application(self, page):
        return ApplicationForm()

    async def fill_application(self, page, plan):
        return FillResult(success=True)

    async def submit_application(self, page):
        assert self.current_ref is not None
        self.submitted.append(self.current_ref.external_id)
        return SubmissionResult(status="submitted", message="synthetic submit")

    async def verify_submission(self, page):
        return SubmissionResult(status="unknown", message="not submitted")


class DelayedEvaluator:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.completed: list[str] = []

    async def __call__(self, posting, *_args, **_kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        self.completed.append(posting.external_id)
        return JobEvaluation(
            decision="apply", score=90, confidence=0.9,
            category="Synthetic role", reason="fixed-delay benchmark",
        )


async def _run_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    case_name: str,
    prefetch_limit: int,
    jobs: list[JobRef],
    model_delay: float,
    browser_delay: float,
) -> dict:
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{case_name}.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        item = JobSession(
            adapter_id="hh", status=SessionStatus.CREATED, counters={},
            application_limit=None, cover_letter_auto=False,
            recovery={"search_filters": {}},
        )
        db.add(item)
        db.flush()
        snapshot = _normalize_extracted(
            {
                "external_id": "benchmark",
                "identity": {"full_name": "Synthetic Candidate", "gender": "male"},
                "target": {"title": "Software Engineer"},
                "about": "Synthetic benchmark profile",
                "skills": [{"name": "Python"}],
            },
            adapter_id="hh",
            source_url="https://hh.example/resume/benchmark",
        )
        persist_session_snapshot(db, item.id, snapshot)
        session_id = item.id
        db.commit()

    adapter = HHWorkflowMock(jobs, browser_delay)
    evaluator = DelayedEvaluator(model_delay)
    monkeypatch.setattr(workflow, "SessionLocal", sessions)
    monkeypatch.setattr(workflow, "EVALUATION_QUEUE_LIMIT", prefetch_limit)
    monkeypatch.setattr(workflow, "AdaptiveSearch", lambda raw, *_args, **_kwargs: raw)
    monkeypatch.setattr(workflow.adapter_registry, "get", lambda _site: adapter)
    monkeypatch.setattr(workflow, "get_browser", lambda _id: SimpleNamespace(page=object()))
    monkeypatch.setattr(workflow, "ModelGateway", lambda: object())
    monkeypatch.setattr(workflow, "evaluate", evaluator)
    monkeypatch.setattr(workflow, "validate_cover_letter", lambda *_args, **_kwargs: (True, None))

    async def letter(*_args, **_kwargs):
        return "Здравствуйте! Буду рад обсудить роль."

    async def complete(*_args, **_kwargs):
        submission = await adapter.submit_application(None)
        return ApplicationOutcome(submission=submission)

    monkeypatch.setattr(workflow, "write_cover_letter", letter)
    monkeypatch.setattr(workflow, "complete_application", complete)
    manager = workflow.WorkflowManager()
    manager._model_gateway = lambda *_args: object()
    started = perf_counter()
    await asyncio.wait_for(manager._run(session_id), timeout=20)
    seconds = perf_counter() - started

    with sessions() as db:
        session = db.get(JobSession, session_id)
        vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == session_id)))
        submissions = db.scalar(select(func.count(Application.id)))
        duplicate_events = db.scalar(select(func.count(BrowserEvent.id)).where(
            BrowserEvent.session_id == session_id,
            BrowserEvent.event_type == "duplicate",
        ))
        result = {
            "seconds": seconds,
            "jobs": len(vacancies),
            "submissions": int(submissions or 0),
            "adapter_submissions": len(adapter.submitted),
            "unique_adapter_submissions": len(set(adapter.submitted)),
            "duplicates": int(duplicate_events or 0),
            "lost_jobs": len(jobs) - len(vacancies),
            "max_model_concurrency": evaluator.max_active,
            "max_browser_concurrency": adapter.max_browser,
            "terminal_states": sorted(vacancy.state for vacancy in vacancies),
            "session_status": str(session.status),
        }
    engine.dispose()
    return result


@pytest.mark.asyncio
async def test_hh_workflow_benchmark_overlaps_model_work_without_browser_races(tmp_path, monkeypatch):
    jobs = [
        JobRef(external_id=f"job-{index}", url=f"https://hh.example/vacancy/{index}")
        for index in range(9)
    ]
    model_delay = 0.30
    browser_delay = 0.004
    sequential = await _run_case(
        tmp_path, monkeypatch, case_name="sequential", prefetch_limit=1,
        jobs=jobs, model_delay=model_delay, browser_delay=browser_delay,
    )
    overlapped = await _run_case(
        tmp_path, monkeypatch, case_name="overlapped", prefetch_limit=10,
        jobs=jobs, model_delay=model_delay, browser_delay=browser_delay,
    )
    improvement = (sequential["seconds"] - overlapped["seconds"]) / sequential["seconds"]
    artifact = {
        "benchmark": "hh-workflow-fixed-model-delay-v1",
        "jobs_requested": len(jobs),
        "fixed_model_delay_seconds": model_delay,
        "fixed_browser_step_delay_seconds": browser_delay,
        "sequential": sequential,
        "overlapped": overlapped,
        "throughput_improvement_percent": round(improvement * 100, 2),
        "no_loss": overlapped["lost_jobs"] == 0,
        "no_duplicate_submissions": (
            overlapped["adapter_submissions"] == overlapped["unique_adapter_submissions"]
        ),
    }
    (tmp_path / "hh-workflow-benchmark.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True))

    assert sequential["jobs"] == overlapped["jobs"] == len(jobs)
    assert sequential["submissions"] == overlapped["submissions"] == len(jobs)
    assert overlapped["duplicates"] == 0
    assert overlapped["lost_jobs"] == 0
    assert overlapped["adapter_submissions"] == overlapped["unique_adapter_submissions"]
    assert overlapped["max_model_concurrency"] > 1
    assert sequential["max_browser_concurrency"] == overlapped["max_browser_concurrency"] == 1
    assert improvement >= 0.30
