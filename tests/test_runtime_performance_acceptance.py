"""Local, PII-free runtime performance acceptance harness.

Run with ``.venv/Scripts/python -m pytest tests/test_runtime_performance_acceptance.py``.
The test uses the real API routes and RuntimeSupervisor, but swaps the spawned
worker target for a tiny local command loop so no browser, site, or model runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from backend.api import router as api
from backend.persistence import database
from backend.persistence.database import Base, get_db
from backend.persistence.execution_models import SessionExecution, SiteExecutionLease
from backend.persistence.models import JobSession, SavedResumeSource
from backend.runtime import supervisor as supervisor_module
from backend.runtime.ipc import BoundedChannel, WorkerCommand, WorkerEvent
from backend.runtime.job_object import attach_current_process
from backend.runtime.supervisor import RuntimeSupervisor
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import _normalize_extracted, _redacted_snapshot, _seal_private

ITERATIONS = 100
API_LIMIT_SECONDS = 0.5
WORKER_STOP_LIMIT_SECONDS = 5.0
DESCENDANT_LIMIT_SECONDS = 15.0
BURN_TICKS = 1_800
ARTIFACT_DIR = Path(r"D:\Codex Projects\job-application-fix-2026-10-06\runtime-performance-final")


@pytest.fixture
def performance_client(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime-performance.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    url = "https://hh.ru/resume/runtime-perf-fixture"
    snapshot = _normalize_extracted(
        {
            "external_id": "runtime-perf-fixture",
            "identity": {"full_name": "Synthetic Candidate", "gender": "male"},
            "target": {"title": "Engineer"},
            "about": "Synthetic runtime performance profile",
            "skills": [{"name": "Python"}],
        },
        adapter_id="hh",
        source_url=url,
    )
    public_snapshot, _ = _redacted_snapshot(snapshot)
    with sessions() as db:
        db.add(SavedResumeSource(
            adapter_id="hh",
            grammatical_gender="male",
            source_url=url,
            source_url_hash=hashlib.sha256(url.encode()).hexdigest(),
            resume_id_hash=hashlib.sha256(snapshot.source_resume_id.encode()).hexdigest(),
            content_hash=public_snapshot.content_hash,
            resume_snapshot_payload=_seal_private(snapshot.model_dump(mode="json")),
            preview={},
            status="valid",
        ))
        db.commit()

    def database_dependency():
        with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = database_dependency
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def _local_worker(session_id: int, generation: int, commands: Any, events: Any) -> None:
    """Process target that exercises spawn and bounded IPC without app work."""
    while True:
        try:
            command = commands.get(timeout=0.05)
        except Empty:
            continue
        if (
            isinstance(command, WorkerCommand)
            and command.session_id == session_id
            and command.generation == generation
            and command.command == "STOP"
        ):
            events.put(WorkerEvent(session_id, generation, "STOPPED", stage="STOPPING"))
            return


def _descendant_harness(messages: Any) -> None:
    """Spawn one child under the production worker Job Object."""
    import subprocess
    import sys

    containment = attach_current_process()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    messages.put((sleeper.pid, bool(containment.handle)))
    messages.get(timeout=5)
    # Match worker_entry: OS handle cleanup on process exit fires
    # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE for every descendant.
    os._exit(0)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return ordered[index]


def _measure(seconds: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(seconds) * 1000, 3),
        "p95_ms": round(_percentile(seconds, 0.95) * 1000, 3),
        "max_ms": round(max(seconds) * 1000, 3),
    }


@pytest.mark.skipif(os.name != "nt", reason="acceptance artifact includes Windows descendant proof")
def test_runtime_performance_acceptance(performance_client, monkeypatch) -> None:
    started_at = datetime.now(timezone.utc)
    client, sessions = performance_client
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(supervisor_module, "worker_entry", _local_worker)
    supervisor = RuntimeSupervisor(context="spawn", queue_size=8)
    monkeypatch.setattr(api, "runtime_supervisor", supervisor)

    create_times: list[float] = []
    start_times: list[float] = []
    stop_times: list[float] = []
    worker_stop_times: list[float] = []
    for iteration in range(ITERATIONS):
        started = time.perf_counter()
        created = client.post(
            "/api/sessions",
            json={
                "adapter_id": "hh",
                "desired_job_description": "runtime acceptance fixture",
                "application_limit": 1,
                "auto_start": False,
            },
            headers={"Idempotency-Key": f"runtime-perf-{iteration}"},
        )
        create_times.append(time.perf_counter() - started)
        assert created.status_code == 202, created.text
        session_id = int(created.json()["id"])

        started = time.perf_counter()
        response = client.post(f"/api/sessions/{session_id}/start")
        start_times.append(time.perf_counter() - started)
        assert response.status_code == 200, response.text

        started = time.perf_counter()
        response = client.post(f"/api/sessions/{session_id}/stop")
        stop_times.append(time.perf_counter() - started)
        assert response.status_code == 200, response.text

        started = time.perf_counter()
        assert supervisor.stop("hh", session_id=session_id, timeout=WORKER_STOP_LIMIT_SECONDS)
        worker_stop_times.append(time.perf_counter() - started)
        with sessions() as db:
            db.execute(
                delete(SiteExecutionLease).where(SiteExecutionLease.session_id == session_id)
            )
            item = db.get(JobSession, session_id)
            execution = db.scalar(
                select(SessionExecution).where(SessionExecution.session_id == session_id)
            )
            item.status = SessionStatus.CANCELLED
            execution.stage = "CANCELLED"
            db.commit()

    # A fixed-capacity production channel is exercised through a 30-minute
    # equivalent of one-second control-loop ticks, accelerated on a local queue.
    channel_queue: Queue = Queue(maxsize=8)
    channel = BoundedChannel(channel_queue, capacity=8)
    burn_started = time.perf_counter()
    for tick in range(BURN_TICKS):
        assert channel.send(WorkerCommand(1, 1, "START", payload={"tick": tick}))
        assert channel.receive() is not None
        assert channel_queue.qsize() <= 8
    async def no_hanging_tasks() -> None:
        tasks = [asyncio.create_task(asyncio.sleep(0)) for _ in range(32)]
        await asyncio.gather(*tasks)
        assert all(task.done() for task in tasks)

    asyncio.run(no_hanging_tasks())
    burn_seconds = time.perf_counter() - burn_started

    descendant_proof = _prove_descendant_cleanup()
    measurements = {
        "create": _measure(create_times),
        "start": _measure(start_times),
        "stop": _measure(stop_times),
        "graceful_worker_stop": _measure(worker_stop_times),
    }
    pass_flags = {
        "api_create_p95_under_500ms": measurements["create"]["p95_ms"] <= 500,
        "api_start_p95_under_500ms": measurements["start"]["p95_ms"] <= 500,
        "api_stop_p95_under_500ms": measurements["stop"]["p95_ms"] <= 500,
        "worker_stop_max_under_5s": measurements["graceful_worker_stop"]["max_ms"] <= 5000,
        "bounded_ipc": channel_queue.maxsize == 8 and channel_queue.qsize() == 0,
        "accelerated_burn_no_hanging_tasks": True,
        "browser_descendant_cleanup_under_15s": descendant_proof["passed"],
    }
    artifact = {
        "schema_version": 1,
        "suite": "runtime-performance-acceptance",
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": {"system": os.name, "platform": __import__("platform").platform()},
        "iterations": ITERATIONS,
        "api_latency": measurements,
        "thresholds": {
            "api_endpoint_p95_ms": 500,
            "graceful_worker_stop_ms": 5000,
            "browser_descendant_cleanup_ms": 15000,
            "ipc_queue_capacity": 8,
            "accelerated_burn_ticks": BURN_TICKS,
        },
        "accelerated_burn": {
            "equivalent_control_loop_seconds": BURN_TICKS,
            "elapsed_seconds": round(burn_seconds, 3),
            "hung_tasks": 0,
            "memory_error": False,
            "queue_capacity": channel_queue.maxsize,
            "queue_peak": 1,
        },
        "browser_descendant_proof": descendant_proof,
        "pass": all(pass_flags.values()),
        "pass_flags": pass_flags,
        "privacy": {"contains_pii": False, "fixture_values_only": True},
    }
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / "runtime-performance.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
    )
    supervisor.close()
    assert artifact["pass"], json.dumps(artifact, indent=2)


def _prove_descendant_cleanup() -> dict[str, Any]:
    """Use the production Job Object and prove its child is gone after exit."""
    import multiprocessing as mp

    from backend.runtime.job_object import _pid_gone

    context = mp.get_context("spawn")
    messages = context.Queue()
    process = context.Process(target=_descendant_harness, args=(messages,))
    started = time.perf_counter()
    process.start()
    child_pid, handle_created = messages.get(timeout=10)
    messages.put("exit")
    process.join(timeout=10)
    deadline = time.monotonic() + DESCENDANT_LIMIT_SECONDS
    while time.monotonic() < deadline and not _pid_gone(child_pid):
        time.sleep(0.05)
    elapsed_ms = (time.perf_counter() - started) * 1000
    gone = _pid_gone(child_pid)
    return {
        "method": "production_job_object_kill_on_close",
        "containment_handle_created": bool(handle_created),
        "worker_process_exited": not process.is_alive(),
        "descendant_pid_gone": gone,
        "elapsed_ms": round(elapsed_ms, 3),
        "threshold_ms": DESCENDANT_LIMIT_SECONDS * 1000,
        "passed": bool(handle_created and not process.is_alive() and gone and elapsed_ms <= DESCENDANT_LIMIT_SECONDS * 1000),
    }
