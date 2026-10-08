"""Acceptance tests for the process-isolated runtime API contract."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
from queue import Queue as LocalQueue
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.api import router as api
from backend.persistence.database import Base, get_db
from backend.persistence.execution_models import (
    SessionExecution,
    SessionIdempotencyKey,
    SiteExecutionLease,
)
from backend.persistence.models import JobSession, SavedResumeSource
from backend.runtime.ipc import BoundedChannel, WorkerCommand, WorkerEvent
from backend.runtime.lifecycle import cancellation_fence, request_cancel
from backend.runtime.supervisor import RuntimeSupervisor, WorkerHandle
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import (
    _normalize_extracted,
    _redacted_snapshot,
    _seal_private,
    persist_session_snapshot,
)


def _source(adapter_id: str = "hh") -> SavedResumeSource:
    host = {"hh": "hh.ru", "hirehi": "hirehi.ru", "zarplata": "zarplata.ru"}[adapter_id]
    url = f"https://{host}/resume/fixture"
    snapshot = _normalize_extracted(
        {
            "external_id": "fixture",
            "identity": {"full_name": "Synthetic Candidate", "gender": "male"},
            "target": {"title": "Engineer"},
            "about": "Synthetic runtime fixture",
            "skills": [{"name": "Python"}],
        },
        adapter_id=adapter_id,
        source_url=url,
    )
    public, _ = _redacted_snapshot(snapshot)
    return SavedResumeSource(
        adapter_id=adapter_id,
        grammatical_gender="male",
        source_url=url,
        source_url_hash=hashlib.sha256(url.encode()).hexdigest(),
        resume_id_hash=hashlib.sha256(snapshot.source_resume_id.encode()).hexdigest(),
        content_hash=public.content_hash,
        resume_snapshot_payload=_seal_private(snapshot.model_dump(mode="json")),
        preview={},
        status="valid",
    )


class _FakeSupervisor:
    def __init__(self) -> None:
        self.handles: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple[str, int]] = []
        self.worker_starts = 0

    def start(self, *, site_id: str, session_id: int):
        self.calls.append((site_id, session_id))
        current = self.handles.get(site_id)
        if current is not None and current.session_id != session_id:
            return None
        if current is None:
            current = SimpleNamespace(session_id=session_id, generation=1)
            self.handles[site_id] = current
            self.worker_starts += 1
        return current

    def cancel(self, **_kwargs):
        return True


@pytest.fixture
def runtime_client(tmp_path: Path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    # Importing execution_models above registers the durable runtime tables.
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(_source())
        db.commit()

    def database():
        with sessions() as db:
            yield db

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(api, "runtime_supervisor", supervisor)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, sessions, supervisor
    engine.dispose()


def _payload(*, auto_start: bool = False, description: str = "QA") -> dict:
    return {
        "adapter_id": "hh",
        "desired_job_description": description,
        "application_limit": 2,
        "auto_start": auto_start,
    }


def test_post_sessions_returns_202_before_import_and_auto_start_is_durable(runtime_client):
    client, sessions, supervisor = runtime_client
    started = time.monotonic()
    response = client.post("/api/sessions", json=_payload(), headers={"Idempotency-Key": "prepare-1"})
    elapsed = time.monotonic() - started

    assert response.status_code == 202
    assert elapsed < 1.0
    session_id = response.json()["id"]
    with sessions() as db:
        item = db.get(JobSession, session_id)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert item.status == SessionStatus.PREPARING
        assert execution.stage == "PREPARING"
        assert execution.start_requested is False
        assert db.scalar(select(func.count(SessionIdempotencyKey.id))) == 1
    assert len(supervisor.calls) == 1


def test_hirehi_launch_pins_confirmed_cached_content_before_worker_start(runtime_client):
    client, sessions, _supervisor = runtime_client
    with sessions() as db:
        db.add(_source("hirehi"))
        db.commit()

    response = client.post(
        "/api/sessions",
        json={**_payload(), "adapter_id": "hirehi"},
        headers={"Idempotency-Key": "hirehi-fresh-import"},
    )
    assert response.status_code == 202
    session_id = response.json()["id"]
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert execution.source_url == "https://hirehi.ru/resume/fixture"
        assert execution.source_url_hash == hashlib.sha256(execution.source_url.encode()).hexdigest()
        saved = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == "hirehi"))
        assert execution.source_content_hash == saved.content_hash
        pinned = db.scalar(
            select(func.count(api.SessionResumeSnapshot.id)).where(
                api.SessionResumeSnapshot.session_id == session_id
            )
        )
        assert pinned == 1


def test_start_during_preparation_reuses_one_worker_and_never_imports_in_api(runtime_client, monkeypatch):
    client, sessions, supervisor = runtime_client
    monkeypatch.setattr(api, "workflow_manager", SimpleNamespace(launch=lambda *_: pytest.fail("in-process workflow")))
    created = client.post("/api/sessions", json=_payload(), headers={"Idempotency-Key": "prepare-2"})
    session_id = created.json()["id"]

    first = client.post(f"/api/sessions/{session_id}/start")
    assert first.status_code == 200
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert execution.start_requested is True
    # The supervisor returns the existing handle; this cannot create a second
    # import/worker even when /start races with preparation.
    second = client.post(f"/api/sessions/{session_id}/start")
    assert second.status_code == 200
    assert supervisor.worker_starts == 1
    assert supervisor.handles["hh"].session_id == session_id


def test_ready_auto_start_false_remains_startable_after_restart_marker(runtime_client):
    client, sessions, supervisor = runtime_client
    created = client.post("/api/sessions", json=_payload(), headers={"Idempotency-Key": "ready-1"})
    session_id = created.json()["id"]
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        execution.stage = "READY"
        db.commit()
    response = client.post(f"/api/sessions/{session_id}/start")
    assert response.status_code == 200
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert execution.stage == "READY"
        assert execution.start_requested is True
    assert supervisor.worker_starts == 1


def test_idempotency_concurrency_and_payload_conflict(runtime_client):
    from concurrent.futures import ThreadPoolExecutor

    client, sessions, supervisor = runtime_client

    def send():
        return client.post(
            "/api/sessions",
            json=_payload(description="same"),
            headers={"Idempotency-Key": "parallel-key"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _value: send(), range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert len({response.json()["id"] for response in responses}) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(JobSession.id))) == 1
        assert db.scalar(select(func.count(SessionIdempotencyKey.id))) == 1
    mismatch = client.post(
        "/api/sessions",
        json=_payload(description="different"),
        headers={"Idempotency-Key": "parallel-key"},
    )
    assert mismatch.status_code == 409
    assert len(supervisor.calls) == 1


def test_site_conflict_creates_no_session_or_idempotency_row(runtime_client):
    client, sessions, supervisor = runtime_client
    first = client.post("/api/sessions", json=_payload(), headers={"Idempotency-Key": "owner"})
    assert first.status_code == 202
    conflict = client.post(
        "/api/sessions",
        json=_payload(description="other"),
        headers={"Idempotency-Key": "loser"},
    )
    assert conflict.status_code == 409
    with sessions() as db:
        assert db.scalar(select(func.count(JobSession.id))) == 1
        assert db.scalar(select(func.count(SessionIdempotencyKey.id))) == 1
        assert db.scalar(select(func.count(SiteExecutionLease.site_id))) == 1


def test_shared_profile_lease_blocks_preview_and_refresh(runtime_client, monkeypatch):
    client, sessions, _supervisor = runtime_client
    with sessions() as db:
        owner = JobSession(adapter_id="hh", status=SessionStatus.RUNNING)
        db.add(owner)
        db.flush()
        db.add(SiteExecutionLease(site_id="hh", session_id=owner.id, generation=1))
        db.commit()
    monkeypatch.setattr(api, "extract_resume", lambda *_args, **_kwargs: pytest.fail("browser opened"))
    preview = client.post(
        "/api/resume-sources/preview",
        json={"adapter_id": "hh", "resume_url": "https://hh.ru/resume/fixture"},
    )
    refresh = client.post("/api/resume-sources/hh/refresh")
    assert preview.status_code == 409
    assert refresh.status_code == 409


def test_stop_writes_cancellation_fence_and_late_generation_is_ignored(runtime_client):
    _client, sessions, _supervisor = runtime_client
    with sessions() as db:
        item = JobSession(adapter_id="hh", status=SessionStatus.RUNNING)
        db.add(item)
        db.flush()
        execution = SessionExecution(
            session_id=item.id,
            stage="IMPORTING",
            generation=7,
            source_url_hash="a" * 64,
            source_content_hash="b" * 64,
        )
        db.add(execution)
        db.commit()
        request_cancel(db, item.id)
        db.commit()
        assert db.get(JobSession, item.id).status == SessionStatus.STOPPING
        assert db.get(SessionExecution, execution.id).stage == "STOPPING"
        assert cancellation_fence(db, item.id, 7)
        # A prior-generation event cannot clear the durable cancellation fence.
        assert cancellation_fence(db, item.id, 6)


@pytest.mark.asyncio
async def test_workflow_fences_open_fill_and_submit_before_representational_call(runtime_client):
    _client, sessions, _supervisor = runtime_client
    with sessions() as db:
        item = JobSession(adapter_id="hh", status=SessionStatus.RUNNING)
        db.add(item)
        db.flush()
        db.add(SessionExecution(session_id=item.id, stage="STOPPING", generation=8, cancel_requested=True))
        db.commit()
        session_id = item.id
    from backend.orchestrator import workflow

    calls: list[str] = []

    async def representational(name):
        calls.append(name)
        return name

    with sessions() as db:
        for name in ("open_application", "fill_application", "submit_application"):
            allowed, result = await workflow._guarded_representational_call(
                db,
                session_id,
                8,
                lambda name=name: representational(name),
            )
            assert allowed is False
            assert result is None
    assert calls == []


def test_supervisor_discards_prior_generation_events():
    supervisor = RuntimeSupervisor(context="spawn", queue_size=2)
    commands = supervisor.ctx.Queue(maxsize=2)
    events = supervisor.ctx.Queue(maxsize=2)
    events.put(WorkerEvent(1, 6, "COMPLETED"))
    process = SimpleNamespace(is_alive=lambda: True, pid=1)
    supervisor.workers["hh"] = WorkerHandle(
        "hh", 1, 7, process, BoundedChannel(commands, capacity=2), BoundedChannel(events, capacity=2)
    )
    assert supervisor.poll("hh") == []


def _runtime_session(sessions, *, auto_start: bool = False, adapter_id: str = "hh"):
    """Create the durable rows consumed by the production worker loop."""
    host = {"hh": "hh.ru", "hirehi": "hirehi.ru", "zarplata": "zarplata.ru"}[adapter_id]
    source_url = f"https://{host}/resume/worker-fixture"
    snapshot = _normalize_extracted(
        {
            "external_id": "worker-fixture",
            "identity": {"full_name": "Test Candidate", "gender": "male"},
            "contacts": {"email": "candidate@example.test"},
            "target": {"title": "Engineer"},
            "about": "Fixture background",
            "skills": [{"name": "Python"}],
        },
        adapter_id=adapter_id,
        source_url=source_url,
    )
    public_snapshot, _ = _redacted_snapshot(snapshot)
    with sessions() as db:
        item = JobSession(adapter_id=adapter_id, status=SessionStatus.PREPARING, counters={})
        db.add(item)
        db.flush()
        source = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id))
        if source is None:
            source = _source(adapter_id)
            db.add(source)
            db.flush()
        source.source_url = source_url
        source.source_url_hash = snapshot.source_url_hash
        source.resume_id_hash = hashlib.sha256(snapshot.source_resume_id.encode()).hexdigest()
        source.content_hash = public_snapshot.content_hash
        source.resume_snapshot_payload = _seal_private(snapshot.model_dump(mode="json"))
        execution = SessionExecution(
            session_id=item.id,
            stage="PREPARING",
            generation=4,
            source_url=source_url,
            source_url_hash=snapshot.source_url_hash,
            source_content_hash=public_snapshot.content_hash,
            start_requested=auto_start,
        )
        db.add(execution)
        db.commit()
        return item.id, snapshot


async def _wait_until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_production_worker_uses_hirehi_copy_and_auto_start_false_is_one_shot(
    runtime_client, monkeypatch
):
    _client, sessions, _supervisor = runtime_client
    session_id, snapshot = _runtime_session(sessions, adapter_id="hirehi")
    import_calls = 0
    workflow_calls = 0

    async def must_not_import(*_args, **_kwargs):
        nonlocal import_calls
        import_calls += 1
        raise AssertionError("worker re-read the HireHi resume")

    class FakeWorkflow:
        def __init__(self, *, generation=None):
            self.generation = generation

        async def run(self, _session_id):
            nonlocal workflow_calls
            workflow_calls += 1

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker
    from backend.services import resume_session

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(resume_session, "revalidate_saved_resume_source", must_not_import)
    monkeypatch.setattr(workflow_module, "WorkflowManager", FakeWorkflow)
    commands = LocalQueue()
    events = LocalQueue()
    worker_task = asyncio.create_task(
        worker._serve(session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8))
    )
    commands.put(WorkerCommand(session_id, 4, "START"))
    def ready():
        with sessions() as db:
            execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
            return execution is not None and execution.stage == "READY"

    await _wait_until(ready)
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        imported = db.scalar(
            select(api.SessionResumeSnapshot).where(
                api.SessionResumeSnapshot.session_id == session_id
            )
        )
        assert execution.stage == "READY"
        assert execution.source_content_hash == imported.content_hash
        assert imported.content_hash == _redacted_snapshot(snapshot)[0].content_hash
        assert imported.full_snapshot["source_site"] == "hirehi"
        assert db.scalar(select(func.count(JobSession.id))) == 1
    assert workflow_calls == 0
    commands.put(WorkerCommand(session_id, 4, "START"))
    await asyncio.wait_for(worker_task, timeout=3)
    assert import_calls == 0
    assert workflow_calls == 1
    with sessions() as db:
        assert db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id)).generation == 4


@pytest.mark.asyncio
async def test_hirehi_worker_keeps_frozen_snapshot_after_source_content_refresh(runtime_client, monkeypatch):
    _client, sessions, _supervisor = runtime_client
    session_id, frozen_snapshot = _runtime_session(sessions, auto_start=True, adapter_id="hirehi")
    changed_snapshot = _normalize_extracted(
        {
            "external_id": "worker-fixture",
            "identity": {"full_name": "Updated Candidate", "gender": "male"},
            "target": {"title": "Updated Engineer"},
            "about": "Updated synthetic candidate profile",
        },
        adapter_id="hirehi",
        source_url="https://hirehi.ru/resume/worker-fixture",
    )
    with sessions() as db:
        persist_session_snapshot(db, session_id, frozen_snapshot)
        source = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == "hirehi"))
        source.resume_snapshot_payload = _seal_private(changed_snapshot.model_dump(mode="json"))
        source.content_hash = _redacted_snapshot(changed_snapshot)[0].content_hash
        db.commit()

    async def must_not_reimport(*_args, **_kwargs):
        pytest.fail("worker reopened a refreshed HireHi source")

    class FakeWorkflow:
        def __init__(self, *, generation=None):
            pass

        async def run(self, _session_id):
            with sessions() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.COMPLETED
                item.finished_at = datetime.now(timezone.utc)
                db.commit()

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker
    from backend.services import resume_session

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(resume_session, "revalidate_saved_resume_source", must_not_reimport)
    monkeypatch.setattr(workflow_module, "WorkflowManager", FakeWorkflow)
    commands = LocalQueue()
    events = LocalQueue()
    task = asyncio.create_task(
        worker._serve(session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8))
    )
    commands.put(WorkerCommand(session_id, 4, "START"))
    await asyncio.wait_for(task, timeout=3)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        stored = db.scalar(select(api.SessionResumeSnapshot).where(
            api.SessionResumeSnapshot.session_id == session_id
        ))
        assert item.status == SessionStatus.COMPLETED
        assert execution.stage == "COMPLETED"
        assert stored.full_snapshot["identity"]["full_name"]["value"] == "Test Candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_id", ["hh", "hirehi", "zarplata"])
async def test_cached_snapshot_tampering_fails_closed_without_reimport(
    runtime_client, monkeypatch, adapter_id
):
    _client, sessions, _supervisor = runtime_client
    session_id, snapshot = _runtime_session(sessions, adapter_id=adapter_id)
    with sessions() as db:
        persist_session_snapshot(db, session_id, snapshot)
        stored = db.scalar(
            select(api.SessionResumeSnapshot).where(
                api.SessionResumeSnapshot.session_id == session_id
            )
        )
        changed_full = dict(stored.full_snapshot)
        changed_target = dict(changed_full["target"])
        changed_title = dict(changed_target["desired_title"])
        changed_title["value"] = "Tampered title"
        changed_target["desired_title"] = changed_title
        changed_full["target"] = changed_target
        stored.full_snapshot = changed_full
        db.commit()

    async def must_not_reimport(*_args, **_kwargs):
        pytest.fail("cached snapshot recovery attempted a network import")

    class FakeWorkflow:
        def __init__(self, *, generation=None):
            pass

        async def run(self, _session_id):
            pytest.fail("workflow started with a tampered snapshot")

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker
    from backend.services import resume_session

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(resume_session, "revalidate_saved_resume_source", must_not_reimport)
    monkeypatch.setattr(workflow_module, "WorkflowManager", FakeWorkflow)
    commands = LocalQueue()
    events = LocalQueue()
    task = asyncio.create_task(
        worker._serve(session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8))
    )
    commands.put(WorkerCommand(session_id, 4, "START"))
    await asyncio.wait_for(task, timeout=3)
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.FAILED
        assert db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id)).stage == "FAILED"


@pytest.mark.asyncio
async def test_production_worker_cancelled_before_cached_preparation_cannot_persist_snapshot(
    runtime_client, monkeypatch
):
    _client, sessions, _supervisor = runtime_client
    session_id, _snapshot = _runtime_session(sessions, adapter_id="hirehi")

    async def must_not_reimport(*_args, **_kwargs):
        pytest.fail("cached HireHi preparation attempted a network import")

    class FakeWorkflow:
        def __init__(self, *, generation=None):
            self.generation = generation

        async def run(self, _session_id):
            pytest.fail("workflow started after cancellation")

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker
    from backend.services import resume_session

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(resume_session, "revalidate_saved_resume_source", must_not_reimport)
    monkeypatch.setattr(workflow_module, "WorkflowManager", FakeWorkflow)
    commands = LocalQueue()
    events = LocalQueue()
    with sessions() as db:
        request_cancel(db, session_id)
        db.commit()
    worker_task = asyncio.create_task(
        worker._serve(session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8))
    )
    commands.put(WorkerCommand(session_id, 4, "START"))
    commands.put(WorkerCommand(session_id, 4, "STOP"))
    await asyncio.wait_for(worker_task, timeout=3)
    with sessions() as db:
        item = db.get(JobSession, session_id)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert item.status == "CANCELLED"
        assert execution.stage == "CANCELLED"
        assert db.scalar(select(func.count(api.SessionResumeSnapshot.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_id", ["hh", "hirehi", "zarplata"])
async def test_production_worker_recovery_uses_existing_immutable_snapshot_without_reimport(
    runtime_client, monkeypatch, adapter_id
):
    _client, sessions, _supervisor = runtime_client
    session_id, snapshot = _runtime_session(sessions, auto_start=True, adapter_id=adapter_id)
    with sessions() as db:
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()
        before = db.scalar(select(api.SessionResumeSnapshot).where(api.SessionResumeSnapshot.session_id == session_id))
        expected_url_hash = before.source_url_hash
        expected_content_hash = before.content_hash
    import_calls = 0
    workflow_calls = 0

    async def must_not_reimport(*_args, **_kwargs):
        nonlocal import_calls
        import_calls += 1
        raise AssertionError("immutable snapshot recovery re-imported source")

    class FakeWorkflow:
        def __init__(self, *, generation=None):
            self.generation = generation

        async def run(self, _session_id):
            nonlocal workflow_calls
            workflow_calls += 1

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker
    from backend.services import resume_session

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(resume_session, "revalidate_saved_resume_source", must_not_reimport)
    monkeypatch.setattr(workflow_module, "WorkflowManager", FakeWorkflow)
    commands = LocalQueue()
    events = LocalQueue()
    task = asyncio.create_task(
        worker._serve(session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8))
    )
    commands.put(WorkerCommand(session_id, 4, "START"))
    await asyncio.wait_for(task, timeout=3)
    assert import_calls == 0
    assert workflow_calls == 1
    with sessions() as db:
        snapshots = list(db.scalars(select(api.SessionResumeSnapshot).where(api.SessionResumeSnapshot.session_id == session_id)))
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert len(snapshots) == 1
        assert snapshots[0].source_url_hash == expected_url_hash
        assert snapshots[0].content_hash == expected_content_hash
        assert execution.source_url_hash == expected_url_hash
        assert execution.source_content_hash == expected_content_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_snapshot", [False, True], ids=["local-cache", "immutable-snapshot"])
async def test_running_stage_is_durable_before_workflow_starts(
    runtime_client, monkeypatch, cached_snapshot
):
    _client, sessions, _supervisor = runtime_client
    session_id, snapshot = _runtime_session(sessions, auto_start=True, adapter_id="hirehi")
    if cached_snapshot:
        with sessions() as db:
            persist_session_snapshot(db, session_id, snapshot)
            db.commit()
    entered = asyncio.Event()

    class BlockingWorkflow:
        def __init__(self, *, generation=None):
            self.generation = generation

        async def run(self, _session_id):
            with sessions() as db:
                item = db.get(JobSession, session_id)
                execution = db.scalar(select(SessionExecution).where(
                    SessionExecution.session_id == session_id
                ))
                assert item.status == SessionStatus.RUNNING
                assert execution.stage == "RUNNING"
                assert execution.wait_reason is None
            entered.set()
            await asyncio.Event().wait()

    async def import_snapshot(db, adapter_id):
        assert not cached_snapshot, "recovery must keep the immutable input"
        return db.scalar(select(SavedResumeSource).where(
            SavedResumeSource.adapter_id == adapter_id
        )), snapshot

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker
    from backend.services import resume_session

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(workflow_module, "WorkflowManager", BlockingWorkflow)
    monkeypatch.setattr(resume_session, "revalidate_saved_resume_source", import_snapshot)
    commands, events = LocalQueue(), LocalQueue()
    task = asyncio.create_task(worker._serve(
        session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8)
    ))
    commands.put(WorkerCommand(session_id, 4, "START"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert not task.done()
    finally:
        commands.put(WorkerCommand(session_id, 4, "STOP"))
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_paused_worker_keeps_browser_owner_and_resumes_without_false_completion(
    runtime_client, monkeypatch
):
    _client, sessions, _supervisor = runtime_client
    session_id, snapshot = _runtime_session(sessions, auto_start=True, adapter_id="hirehi")
    with sessions() as db:
        persist_session_snapshot(db, session_id, snapshot)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        # The execution table's primary key is not the session key. A worker
        # lookup must use the unique session_id column even when it differs.
        execution.id += 10_000
        decoy = JobSession(adapter_id="hh", status=SessionStatus.PREPARING)
        db.add(decoy)
        db.flush()
        db.add(SessionExecution(
            id=session_id,
            session_id=decoy.id,
            generation=77,
            stage="DECOY",
        ))
        db.commit()

    run_calls = 0

    class FakeWorkflow:
        def __init__(self, *, generation=None):
            self.generation = generation

        async def run(self, _session_id):
            nonlocal run_calls
            run_calls += 1
            with sessions() as db:
                item = db.get(JobSession, session_id)
                execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
                assert execution.stage == "RUNNING"
                if run_calls == 1:
                    item.status = SessionStatus.PAUSED
                    item.stop_reason = "CAPTCHA требуется проверка"
                    execution.stage = "PAUSED"
                else:
                    item.status = SessionStatus.COMPLETED
                    item.finished_at = datetime.now(timezone.utc)
                    execution.stage = "COMPLETED"
                db.commit()

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(workflow_module, "WorkflowManager", FakeWorkflow)
    browser_commands = []

    async def browser_command(command, _events, **_kwargs):
        browser_commands.append(command.command)

    monkeypatch.setattr(worker, "_handle_open_browser_command", browser_command)
    commands, events = LocalQueue(), LocalQueue()
    worker_task = asyncio.create_task(worker._serve(
        session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8)
    ))
    commands.put(WorkerCommand(session_id, 4, "START"))

    await _wait_until(lambda: _durable_status(sessions, session_id) == SessionStatus.PAUSED)
    assert not worker_task.done()
    with sessions() as db:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert execution.stage == "PAUSED"
        assert db.get(SiteExecutionLease, "hirehi") is not None
        assert db.get(SessionExecution, session_id).stage == "DECOY"
        assert db.get(SessionExecution, session_id).generation == 77

    # START and an older generation's RESUME cannot clear a CAPTCHA pause.
    commands.put(WorkerCommand(session_id, 4, "START"))
    commands.put(WorkerCommand(session_id, 3, "RESUME"))
    commands.put(WorkerCommand(session_id, 4, "OPEN_BROWSER"))
    await _wait_until(lambda: browser_commands == ["OPEN_BROWSER"])
    assert run_calls == 1
    assert _durable_status(sessions, session_id) == SessionStatus.PAUSED
    with sessions() as db:
        db.get(JobSession, session_id).status = SessionStatus.PREPARING
        db.commit()
    commands.put(WorkerCommand(session_id, 4, "RESUME"))
    await asyncio.wait_for(worker_task, timeout=3)

    assert run_calls == 2
    assert browser_commands == ["OPEN_BROWSER"]
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.COMPLETED
        assert db.get(SiteExecutionLease, "hirehi") is None
    observed = []
    while True:
        try:
            observed.append(events.get_nowait().event)
        except Empty:
            break
    assert "PAUSED" in observed
    assert "COMPLETED" in observed
    assert observed.count("COMPLETED") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [SessionStatus.COMPLETED, SessionStatus.FAILED])
async def test_stop_command_preserves_already_terminal_status_and_execution_stage(
    runtime_client, monkeypatch, terminal_status
):
    _client, sessions, _supervisor = runtime_client
    session_id, snapshot = _runtime_session(sessions, auto_start=True, adapter_id="hh")
    with sessions() as db:
        persist_session_snapshot(db, session_id, snapshot)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        execution.id += 10_000
        db.commit()

    finished = asyncio.Event()

    class TerminalWorkflow:
        def __init__(self, *, generation=None):
            self.generation = generation

        async def run(self, _session_id):
            with sessions() as db:
                item = db.get(JobSession, session_id)
                execution = db.scalar(select(SessionExecution).where(
                    SessionExecution.session_id == session_id
                ))
                item.status = terminal_status
                item.finished_at = datetime.now(timezone.utc)
                execution.stage = terminal_status
                db.commit()
            finished.set()
            await asyncio.Event().wait()

    from backend.orchestrator import workflow as workflow_module
    from backend.persistence import database
    from backend.runtime import worker

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(workflow_module, "WorkflowManager", TerminalWorkflow)
    commands, events = LocalQueue(), LocalQueue()
    worker_task = asyncio.create_task(worker._serve(
        session_id, 4, BoundedChannel(commands, capacity=8), BoundedChannel(events, capacity=8)
    ))
    commands.put(WorkerCommand(session_id, 4, "START"))
    await asyncio.wait_for(finished.wait(), timeout=3)
    commands.put(WorkerCommand(session_id, 4, "STOP"))
    await asyncio.wait_for(worker_task, timeout=3)

    with sessions() as db:
        item = db.get(JobSession, session_id)
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        assert item.status == terminal_status
        assert execution.stage == terminal_status
        assert execution.cancel_requested is False
        assert db.get(SiteExecutionLease, "hh") is None


def _durable_status(sessions, session_id):
    with sessions() as db:
        item = db.get(JobSession, session_id)
        return item.status if item is not None else None


def _production_entry_with_database(db_path: str, target, target_args) -> None:
    """Configure an isolated DB in the spawned child, then call production target."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.persistence import database, execution_models, models  # noqa: F401
    from backend.persistence.database import Base

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    database.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    target(*target_args)


def _job_object_descendant_harness(messages) -> None:
    """Create a real Job Object and a child process for cleanup acceptance."""
    import subprocess

    from backend.runtime.job_object import attach_current_process

    containment = attach_current_process()
    sleeper = subprocess.Popen(
        [os.fspath(Path(os.sys.executable)), "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    messages.put((sleeper.pid, containment.handle))
    messages.get(timeout=5)
    # The production worker closes its containment handle in ``finally`` as
    # the process exits.  Closing KILL_ON_JOB_CLOSE from inside the job would
    # terminate this harness before it can report, so exercise the same
    # process-exit path and let the parent prove the descendant is gone.
    os._exit(0)


@pytest.mark.skipif(os.name != "nt", reason="Windows production worker boundary")
def test_windows_production_worker_entry_crash_recovery_and_generation(tmp_path: Path, monkeypatch):
    """Run the real worker_entry through spawn and recover its crashed PID."""
    import multiprocessing as mp

    from backend.persistence import database
    from backend.persistence.pipeline_models import PipelineItem  # noqa: F401

    db_path = tmp_path / "production-worker.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    session_id, snapshot = _runtime_session(sessions)
    with sessions() as db:
        persist_session_snapshot(db, session_id, snapshot)
        db.commit()
    monkeypatch.setattr(database, "SessionLocal", sessions)
    real_context = mp.get_context("spawn")
    owned_processes = []
    queues = []

    def cleanup_owned_processes() -> None:
        for process in reversed(owned_processes):
            try:
                if process.pid is None or not process.is_alive():
                    continue
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
            except Exception:
                # Preserve the test failure while still attempting cleanup of
                # every process created by this test.
                continue
        for queue in queues:
            try:
                queue.close()
                queue.join_thread()
            except Exception:
                continue

    class ContextProxy:
        Queue = real_context.Queue

        @staticmethod
        def Process(*, target, args, daemon):
            return real_context.Process(
                target=_production_entry_with_database,
                args=(str(db_path), target, args),
                daemon=daemon,
            )

    supervisor = RuntimeSupervisor(context="spawn", queue_size=4)
    supervisor.ctx = ContextProxy()
    try:
        handle = supervisor.start(site_id="hh", session_id=session_id)
        if handle is not None:
            owned_processes.append(handle.process)
        assert handle is not None
        deadline = time.monotonic() + 15
        worker_pid = None
        while time.monotonic() < deadline:
            with sessions() as db:
                execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
                worker_pid = execution.worker_pid
            if worker_pid:
                break
            time.sleep(0.05)
        assert worker_pid
        assert handle.process.pid == worker_pid
        handle.process.kill()
        handle.process.join(timeout=5)
        assert not handle.process.is_alive()
        with sessions() as db:
            execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
            execution.stage = "PREPARING"
            db.commit()

        restarted = RuntimeSupervisor(context="spawn", queue_size=4)
        restarted.ctx = ContextProxy()
        recovered = restarted.recover()
        assert session_id in recovered
        replacement = restarted.workers["hh"]
        owned_processes.append(replacement.process)
        assert replacement.generation > handle.generation
        assert replacement.process.pid != handle.process.pid
        started = time.monotonic()
        assert restarted.stop("hh", session_id=session_id, timeout=5.0)
        assert time.monotonic() - started <= 5.0

        # The same Windows acceptance also proves the production Job Object's
        # descendant cleanup contract with a real sleeper child.
        messages = real_context.Queue()
        queues.append(messages)
        descendant = real_context.Process(target=_job_object_descendant_harness, args=(messages,))
        descendant.start()
        owned_processes.append(descendant)
        child_pid, containment_handle = messages.get(timeout=5)
        assert containment_handle
        messages.put("exit")
        descendant.join(timeout=5)
        from backend.runtime.job_object import _pid_gone

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _pid_gone(child_pid):
            time.sleep(0.05)
        assert _pid_gone(child_pid)
    finally:
        cleanup_owned_processes()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object descendant acceptance")
def test_windows_job_object_descendant_cleanup_under_five_seconds():
    import multiprocessing as mp

    context = mp.get_context("spawn")
    messages = context.Queue()
    process = context.Process(target=_job_object_descendant_harness, args=(messages,))
    started = time.monotonic()
    process.start()
    child_pid, handle = messages.get(timeout=5)
    assert handle
    messages.put("close")
    process.join(timeout=5)
    from backend.runtime.job_object import _pid_gone

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _pid_gone(child_pid):
        time.sleep(0.05)
    assert _pid_gone(child_pid)
    assert time.monotonic() - started <= 5.0
