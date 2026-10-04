from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.persistence.database import Base
from backend.persistence.execution_models import SessionExecution
from backend.persistence.models import (
    BrowserEvent,
    JobSession,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.runtime.ipc import BoundedChannel, WorkerEvent
from backend.runtime.supervisor import RuntimeSupervisor
from backend.schemas.domain import SessionStatus


class _DeadProcess:
    pid = 987654
    exitcode = -9

    def is_alive(self):
        return False


class _OneMonitorPass:
    def __init__(self):
        self.checked = False

    def is_set(self):
        return self.checked

    async def wait(self):
        self.checked = True


@pytest.fixture
def monitor_runtime(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'terminal.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        item = JobSession(
            id=80,
            adapter_id="hirehi",
            status=SessionStatus.RUNNING,
            counters={"reported": 5, "errors": 0},
            recovery={
                "pending_refs": [{"external_id": "pending"}],
                "pending_questions": ["Question?"],
                "manual_application_vacancy_ids": [22],
                "question_context": {"question": "Question?"},
            },
        )
        db.add(item)
        db.add(SessionExecution(id=180, session_id=80, generation=3, stage="RUNNING"))
        db.add(SessionResumeSnapshot(
            session_id=80,
            source_site="hirehi",
            source_resume_id="fixture",
            source_url_hash="a" * 64,
            content_hash="b" * 64,
            snapshot={},
            professional_view={},
            full_snapshot={},
            private_view="sealed-test:e30=",
        ))
        db.add(Vacancy(
            session_id=80,
            source="hirehi",
            external_id="reported",
            url="https://hirehi.ru/vacancy/reported",
            title="Reported role",
            company="Example",
            state="REPORTED",
            data={"report_route_kind": "direct_contact", "report_contact": "team@example.test"},
        ))
        db.add(Vacancy(
            session_id=80,
            source="hirehi",
            external_id="pending",
            url="https://hirehi.ru/vacancy/pending",
            title="Pending role",
            company="Example",
            state="EXTRACTED",
            data={"description": "fixture"},
        ))
        db.commit()

    def session_local():
        return sessions()

    monkeypatch.setattr("backend.persistence.database.SessionLocal", session_local)
    monkeypatch.chdir(tmp_path)
    supervisor = RuntimeSupervisor()

    def run_monitor(event: WorkerEvent | None, *, worker_alive: bool = False):
        queue = Queue()
        if event is not None:
            queue.put(event)
        supervisor.workers["hirehi"] = SimpleNamespace(
            session_id=80,
            generation=3,
            process=(SimpleNamespace(is_alive=lambda: True) if worker_alive else _DeadProcess()),
            events=BoundedChannel(queue, capacity=8),
        )
        asyncio.run(supervisor.monitor(_OneMonitorPass(), interval=0.001))

    yield sessions, run_monitor
    engine.dispose()


@pytest.mark.parametrize("status", [
    SessionStatus.RUNNING, SessionStatus.PAUSED, SessionStatus.COMPLETED, SessionStatus.FAILED,
])
@pytest.mark.parametrize("queued_stage", ["IMPORTING", "READY"])
def test_delayed_ready_notification_preserves_durable_stage(monitor_runtime, status, queued_stage):
    sessions, run_monitor = monitor_runtime
    transition_time = datetime(2025, 1, 2, 3, 4, 5)
    with sessions() as db:
        db.get(JobSession, 80).status = status
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        execution.stage = status
        execution.stage_started_at = transition_time
        execution.last_progress_at = transition_time
        execution.wait_reason = "durable reason"
        db.commit()

    run_monitor(WorkerEvent(80, 3, "READY", stage=queued_stage), worker_alive=True)

    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        assert db.get(JobSession, 80).status == status
        assert execution.stage == status
        assert execution.stage_started_at == transition_time
        assert execution.last_progress_at == transition_time
        assert execution.wait_reason == "durable reason"


@pytest.mark.parametrize(
    ("event_name", "cancel_requested", "expected_status", "expected_pending", "expected_errors"),
    [
        ("STOPPED", True, SessionStatus.CANCELLED, "CANCELLED", 0),
        ("COMPLETED", False, SessionStatus.COMPLETED, "ERROR", 1),
        ("FAILED", False, SessionStatus.FAILED, "ERROR", 1),
    ],
)
def test_monitor_terminal_worker_event_finalizes_report_and_session(
    monitor_runtime,
    tmp_path,
    monkeypatch,
    event_name,
    cancel_requested,
    expected_status,
    expected_pending,
    expected_errors,
):
    sessions, run_monitor = monitor_runtime
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        execution.cancel_requested = cancel_requested
        if event_name == "COMPLETED":
            db.get(JobSession, 80).status = SessionStatus.COMPLETED
        db.commit()
    run_monitor(WorkerEvent(80, 3, event_name, stage="STOPPING" if event_name == "STOPPED" else ""))

    assert (tmp_path / "output" / "pdf" / "hirehi-session-80.pdf").is_file()
    with sessions() as db:
        item = db.get(JobSession, 80)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == 80)))
        reported = next(row for row in vacancies if row.external_id == "reported")
        pending = next(row for row in vacancies if row.external_id == "pending")
        assert item.status == expected_status
        assert execution.stage == expected_status
        assert item.finished_at is not None
        assert item.counters["reported"] == 5
        assert item.counters["errors"] == expected_errors
        assert reported.state == "REPORTED"
        assert reported.data["report_route_kind"] == "direct_contact"
        assert pending.state == expected_pending
        assert db.scalar(select(SessionResumeSnapshot).where(
            SessionResumeSnapshot.session_id == 80
        )) is not None
        assert "pending_refs" not in item.recovery
        assert "pending_questions" not in item.recovery
        assert "manual_application_vacancy_ids" not in item.recovery
        assert item.recovery["terminal_finalized"] is True
        assert db.scalar(select(func.count(BrowserEvent.id)).where(
            BrowserEvent.session_id == 80,
            BrowserEvent.event_type == "report_ready",
        )) == 1


def test_monitor_unexpected_worker_exit_uses_terminal_finalizer(monitor_runtime, tmp_path):
    sessions, run_monitor = monitor_runtime
    run_monitor(None)

    assert (tmp_path / "output" / "pdf" / "hirehi-session-80.pdf").is_file()
    with sessions() as db:
        item = db.get(JobSession, 80)
        pending = db.scalar(select(Vacancy).where(Vacancy.external_id == "pending"))
        assert item.status == SessionStatus.FAILED
        assert item.stop_reason == "Рабочий процесс завершился неожиданно"
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        assert execution.error == "WORKER_PROCESS_EXITED_UNEXPECTEDLY: exitcode=-9"
        assert pending.state == "ERROR"
        assert item.recovery["terminal_finalized"] is True
        assert db.scalar(select(func.count(BrowserEvent.id)).where(
            BrowserEvent.session_id == 80,
            BrowserEvent.event_type == "report_ready",
        )) == 1


@pytest.mark.parametrize(
    ("status", "expected", "stage"),
    [
        (SessionStatus.RUNNING, SessionStatus.FAILED, "FAILED"),
        (SessionStatus.PREPARING, SessionStatus.FAILED, "FAILED"),
        (SessionStatus.PAUSED, SessionStatus.PAUSED, "PAUSED"),
    ],
)
def test_spurious_completed_event_cannot_override_durable_session_state(
    monitor_runtime, status, expected, stage
):
    sessions, run_monitor = monitor_runtime
    with sessions() as db:
        db.get(JobSession, 80).status = status
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        execution.stage = status
        db.commit()

    run_monitor(WorkerEvent(80, 3, "COMPLETED"))

    with sessions() as db:
        item = db.get(JobSession, 80)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        assert item.status == expected
        assert execution.stage == stage
        if status == SessionStatus.PAUSED:
            assert "paused" in execution.error.lower()
        else:
            assert item.stop_reason == "Рабочий процесс завершился до подтверждения окончания сессии"
            assert execution.error == (
                "PREMATURE_COMPLETED_EVENT: worker reported completion before durable terminal state"
            )


@pytest.mark.parametrize(
    ("status", "stage"),
    [(SessionStatus.COMPLETED, "COMPLETED"), (SessionStatus.FAILED, "FAILED")],
)
def test_stopped_event_preserves_existing_terminal_state(monitor_runtime, status, stage):
    sessions, run_monitor = monitor_runtime
    with sessions() as db:
        db.get(JobSession, 80).status = status
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        execution.stage = stage
        db.commit()

    run_monitor(WorkerEvent(80, 3, "STOPPED"))

    with sessions() as db:
        item = db.get(JobSession, 80)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == 80))
        assert item.status == status
        assert execution.stage == stage
        assert execution.cancel_requested is False


def test_terminal_finalization_import_isolated_from_workflow_and_browser_runtime():
    script = """
import sys
import backend.orchestrator.terminal_finalization
assert 'backend.orchestrator.workflow' not in sys.modules
assert not any(
    name.startswith('playwright') or name.startswith('backend.browser')
    for name in sys.modules
)
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
