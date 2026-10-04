"""PII-free, wall-clock endurance run for the local runtime coordination paths.

The harness uses the production durable broker, pipeline store, and IPC wrapper
with a temporary SQLite database and deterministic local provider. It never
connects to a vacancy site or a real model provider.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import queue
import signal
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from backend.adapters.base.protocol import JobRef
from backend.intelligence.model_broker import (
    ModelRequestBroker,
    ModelVersions,
    ProviderCall,
    SubmitRequest,
)
from backend.intelligence.search_planner import SearchQueryPlan
from backend.orchestrator.pipeline import PipelineStore
from backend.persistence.database import Base
from backend.persistence.model_request_models import ModelRequest
from backend.persistence.models import JobSession
from backend.persistence.pipeline_models import PipelineCheckpoint, PipelineItem
from backend.runtime.ipc import BoundedChannel, WorkerEvent

ARTIFACT_DIR = Path(r"D:\VScode Projects\job-application-test-artifacts\endurance-4h")
GLOBAL_LIMIT = 3
SESSION_LIMIT = 2
SITE_LIMIT = 10
BROKER_BACKLOG_LIMIT = 60
IPC_CAPACITY = 32
SAMPLE_INTERVAL_SECONDS = 10.0
PIPELINE_INTERVAL_SECONDS = 30.0
HUNG_TASK_SECONDS = 30.0
REQUIRED_SLEEP_PREVENTION_SECONDS = 4 * 60 * 60
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
INTERRUPT_REQUESTED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeterministicProvider:
    """Local fake that returns valid schema data and tracks active calls."""

    def __init__(self, latency: float) -> None:
        self.latency = latency
        self.active = 0
        self.max_active = 0
        self.started_calls: dict[str, float] = {}

    async def generate(self, call: ProviderCall) -> BaseModel:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started_calls[call.request_id] = time.monotonic()
        try:
            await asyncio.sleep(self.latency)
            return SearchQueryPlan(queries=[])
        finally:
            self.active -= 1
            self.started_calls.pop(call.request_id, None)

    async def catalog_status(self) -> dict[str, Any]:
        return {"available": True, "provider": "deterministic-local-fake"}


def process_tree_rss() -> dict[str, int]:
    """Return current process plus descendants RSS; no third-party dependency."""
    pid = os.getpid()
    if sys.platform.startswith("linux"):
        rss_by_pid: dict[int, int] = {}
        parent_by_pid: dict[int, int] = {}
        proc = Path("/proc")
        page_size = os.sysconf("SC_PAGE_SIZE")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8").split()
                child_pid = int(fields[0])
                parent_by_pid[child_pid] = int(fields[3])
                rss_by_pid[child_pid] = int(fields[23]) * page_size
            except (OSError, ValueError, IndexError):
                continue
        descendants = {pid}
        changed = True
        while changed:
            changed = False
            for child_pid, parent_pid in parent_by_pid.items():
                if parent_pid in descendants and child_pid not in descendants:
                    descendants.add(child_pid)
                    changed = True
        return {
            "process_rss_bytes": rss_by_pid.get(pid, 0),
            "descendant_rss_bytes": sum(rss_by_pid.get(item, 0) for item in descendants if item != pid),
            "process_tree_rss_bytes": sum(rss_by_pid.get(item, 0) for item in descendants),
            "descendant_count": len(descendants) - 1,
        }
    if os.name == "nt":
        try:
            import psutil

            process = psutil.Process(pid)
            children = process.children(recursive=True)
            return {
                "process_rss_bytes": process.memory_info().rss,
                "descendant_rss_bytes": sum(child.memory_info().rss for child in children if child.is_running()),
                "process_tree_rss_bytes": process.memory_info().rss + sum(
                    child.memory_info().rss for child in children if child.is_running()
                ),
                "descendant_count": len(children),
            }
        except ImportError:
            # The harness creates no subprocesses; current process is the full tree.
            import ctypes
            import ctypes.wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_memory_info.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(Counters), ctypes.wintypes.DWORD]
            get_memory_info.restype = ctypes.wintypes.BOOL
            if not get_memory_info(handle, ctypes.byref(counters), counters.cb):
                raise OSError("GetProcessMemoryInfo failed") from None
            return {
                "process_rss_bytes": int(counters.WorkingSetSize),
                "descendant_rss_bytes": 0,
                "process_tree_rss_bytes": int(counters.WorkingSetSize),
                "descendant_count": 0,
            }
    import resource

    rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        rss *= 1024
    return {
        "process_rss_bytes": rss,
        "descendant_rss_bytes": 0,
        "process_tree_rss_bytes": rss,
        "descendant_count": 0,
    }


class RunState:
    def __init__(self, requested_seconds: float, artifact_dir: Path) -> None:
        self.run_id = str(uuid.uuid4())
        self.requested_seconds = requested_seconds
        self.artifact_dir = artifact_dir
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()
        self.samples: list[dict[str, Any]] = []
        self.completed = 0
        self.failed = 0
        self.errors = 0
        self.memory_error = False
        self.hung_task = False
        self.unbounded = False
        self.session_limit_violation = False
        self.max_broker_queue = 0
        self.max_pipeline_active = 0
        self.max_pipeline_backlog = 0
        self.max_ipc_depth = 0
        self.max_provider_concurrency = 0
        self.tasks: set[asyncio.Task[Any]] = set()
        self.sleep_prevention = {
            "requested": os.name == "nt",
            "active_at_start": False,
            "released": os.name != "nt",
        }
        self.interrupted = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def checkpoint(self, *, final: bool = False, interrupted: bool = False) -> dict[str, Any]:
        elapsed = self.elapsed()
        baseline = self.samples[0]["process_tree_rss_bytes"] if self.samples else 0
        hour_ago = elapsed - 3600.0
        recent = [sample for sample in self.samples if sample["elapsed_seconds"] >= hour_ago]
        last_hour_start = recent[0]["process_tree_rss_bytes"] if recent else baseline
        last_rss = self.samples[-1]["process_tree_rss_bytes"] if self.samples else baseline
        growth = ((last_rss - last_hour_start) / last_hour_start * 100.0) if last_hour_start else 0.0
        flags = {
            "no_errors": self.errors == 0 and self.failed == 0,
            "no_memory_error": not self.memory_error,
            "no_hung_tasks": not self.hung_task,
            "bounded_queues": not self.unbounded,
            "global_provider_concurrency_at_most_3": self.max_provider_concurrency <= GLOBAL_LIMIT,
            "session_provider_concurrency_at_most_2": not self.session_limit_violation,
            "active_evaluation_items_at_most_10_per_site": self.max_pipeline_active <= SITE_LIMIT,
            "pipeline_backlog_at_most_10_per_site": self.max_pipeline_backlog <= SITE_LIMIT,
            "last_hour_rss_growth_at_most_10_percent": growth <= 10.0,
            "actual_duration_at_least_requested": elapsed >= self.requested_seconds,
            "completed_without_interrupt": not interrupted,
        }
        return {
            "schema_version": 1,
            "suite": "runtime-endurance-acceptance",
            "run_id": self.run_id,
            "started_at_utc": self.started_at,
            "ended_at_utc": utc_now() if final else None,
            "requested_duration_seconds": self.requested_seconds,
            "actual_elapsed_seconds": round(elapsed, 3),
            "sample_count": len(self.samples),
            "samples": self.samples,
            "completed": self.completed,
            "failed": self.failed,
            "runtime_errors": self.errors,
            "sleep_prevention": dict(self.sleep_prevention),
            "memory_error": self.memory_error,
            "hung_task": self.hung_task,
            "maxima": {
                "broker_queued": self.max_broker_queue,
                "pipeline_active_per_site": self.max_pipeline_active,
                "pipeline_backlog_per_site": self.max_pipeline_backlog,
                "ipc_depth": self.max_ipc_depth,
                "provider_concurrency": self.max_provider_concurrency,
            },
            "rss": {
                "baseline_process_tree_bytes": baseline,
                "last_hour_start_process_tree_bytes": last_hour_start,
                "last_process_tree_bytes": last_rss,
                "last_hour_growth_percent": round(growth, 3),
            },
            "pass_flags": flags,
            "pass": bool(final and all(flags.values())),
            "incomplete": bool(
                interrupted or not final or elapsed < self.requested_seconds
                or self.errors or self.memory_error
            ),
            "privacy": {"contains_pii": False, "provider": "deterministic-local-fake", "network_access": False},
            "database_volume": self.artifact_dir.drive.rstrip(":") or "unknown",
        }


def set_sleep_prevention(enabled: bool) -> bool:
    """Request or release the Windows system-required execution state."""
    if os.name != "nt":
        return True
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError):
        return False


def persist_checkpoint(state: RunState, *, final: bool = False, interrupted: bool = False) -> dict[str, Any]:
    state.artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = state.checkpoint(final=final, interrupted=interrupted)
    with (state.artifact_dir / "heartbeat.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({key: value for key, value in artifact.items() if key != "samples"}, sort_keys=True) + "\n")
    (state.artifact_dir / "checkpoint.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
    )
    if final:
        (state.artifact_dir / "summary.json").write_text(
            json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
        )
    return artifact


async def _run_harness(state: RunState, duration_seconds: float, artifact_dir: Path, sample_interval: float) -> None:
    interrupted = False
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".runtime-endurance-", dir=artifact_dir) as temp_dir:
        db_path = Path(temp_dir) / "endurance.sqlite3"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as db:
            session_ids = []
            for _ in range(6):
                row = JobSession(adapter_id="fake", desired_job_description="synthetic", status="RUNNING")
                db.add(row)
                db.flush()
                session_ids.append(row.id)
            db.commit()

        provider = DeterministicProvider(latency=0.06)
        broker = ModelRequestBroker(
            sessions,
            provider=provider,
            logical_deadline_seconds=180,
            retry_base_seconds=5,
        )
        pipeline = PipelineStore(sessions)
        ipc_queue: queue.Queue[WorkerEvent] = queue.Queue(maxsize=IPC_CAPACITY)
        channel = BoundedChannel(ipc_queue, capacity=IPC_CAPACITY)
        site_id = "deterministic-site"
        next_pipeline_at = 0.0
        pipeline_batch = 0
        broker_task = asyncio.create_task(broker.run_forever(tick_seconds=0.02))
        state.tasks.add(broker_task)
        next_sample_at = 0.0
        try:
            while state.elapsed() < duration_seconds and not INTERRUPT_REQUESTED:
                loop_started = time.monotonic()
                with sessions() as db:
                    queued = int(db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.status.in_(("queued", "retry")))) or 0)
                    running = int(db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.status == "running")) or 0)
                state.max_broker_queue = max(state.max_broker_queue, queued)

                # Keep a fixed upper bound on durable broker backlog while maintaining load.
                if queued + running < BROKER_BACKLOG_LIMIT:
                    for offset in range(min(12, BROKER_BACKLOG_LIMIT - queued - running)):
                        session_id = session_ids[(state.completed + state.failed + offset) % len(session_ids)]
                        request = SubmitRequest(
                            session_id=session_id,
                            site_id=f"site-{session_id % 3}",
                            stage="evaluation",
                            role="runtime_endurance_fixture",
                            payload={"fixture": "synthetic", "sequence": state.completed + offset},
                            schema=SearchQueryPlan,
                            versions=ModelVersions("fake", "1", "1", "1", "1"),
                            max_attempts=1,
                        )
                        broker.submit(request)

                if time.monotonic() >= next_pipeline_at:
                    ref_count = SITE_LIMIT * 2 if pipeline_batch == 0 else SITE_LIMIT
                    refs = [
                        JobRef(external_id=f"synthetic-{pipeline_batch}-{index}", url=f"https://example.invalid/{pipeline_batch}/{index}")
                        for index in range(ref_count)
                    ]
                    pipeline.enqueue(session_ids[0], site_id, refs, generation=0)
                    metrics = pipeline.queue_metrics(site_id, generation=0)
                    active = metrics["active"]
                    state.max_pipeline_active = max(state.max_pipeline_active, active)
                    with sessions() as db:
                        checkpoint = db.scalar(select(PipelineCheckpoint).where(
                            PipelineCheckpoint.session_id == session_ids[0],
                            PipelineCheckpoint.site_id == site_id,
                            PipelineCheckpoint.name == "discovery",
                        ))
                        backlog = len((checkpoint.data or {}).get("backlog", [])) if checkpoint else 0
                        state.max_pipeline_backlog = max(state.max_pipeline_backlog, backlog)
                        if active > SITE_LIMIT or backlog > SITE_LIMIT:
                            state.unbounded = True
                        db.execute(update(PipelineItem).where(
                            PipelineItem.site_id == site_id,
                            PipelineItem.session_id == session_ids[0],
                            PipelineItem.generation == 0,
                            PipelineItem.status.in_(("queued", "running")),
                        ).values(status="completed", stage="completed"))
                        db.commit()
                    pipeline_batch += 1
                    next_pipeline_at = time.monotonic() + PIPELINE_INTERVAL_SECONDS

                # Saturate the fixed IPC queue once per loop, then drain it.
                for index in range(IPC_CAPACITY + 1):
                    event = WorkerEvent(session_ids[0], 0, "HEARTBEAT", payload={"tick": index})
                    channel.send(event)
                state.max_ipc_depth = max(state.max_ipc_depth, ipc_queue.qsize())
                while channel.receive() is not None:
                    pass

                with sessions() as db:
                    state.completed = int(db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.status == "completed")) or 0)
                    state.failed = int(db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.status == "failed")) or 0)
                    running_by_session = db.execute(
                        select(ModelRequest.session_id, func.count(ModelRequest.id))
                        .where(ModelRequest.status == "running")
                        .group_by(ModelRequest.session_id)
                    ).all()
                    current_pipeline = int(db.scalar(select(func.count(PipelineItem.id)).where(
                        PipelineItem.site_id == site_id,
                        PipelineItem.stage.in_(("discovery", "extraction", "evaluation")),
                        PipelineItem.status.in_(("queued", "running")),
                    )) or 0)
                    current_checkpoint = db.scalar(select(PipelineCheckpoint).where(
                        PipelineCheckpoint.session_id == session_ids[0],
                        PipelineCheckpoint.site_id == site_id,
                        PipelineCheckpoint.name == "discovery",
                    ))
                    current_backlog = len((current_checkpoint.data or {}).get("backlog", [])) if current_checkpoint else 0
                concurrency = sum(count for _, count in running_by_session)
                state.max_provider_concurrency = max(state.max_provider_concurrency, provider.max_active, concurrency)
                state.max_pipeline_active = max(state.max_pipeline_active, current_pipeline)
                state.max_pipeline_backlog = max(state.max_pipeline_backlog, current_backlog)
                if queued + running > BROKER_BACKLOG_LIMIT or ipc_queue.qsize() > IPC_CAPACITY:
                    state.unbounded = True
                if current_pipeline > SITE_LIMIT or current_backlog > SITE_LIMIT:
                    state.unbounded = True
                if any(count > SESSION_LIMIT for _, count in running_by_session):
                    state.unbounded = True
                    state.session_limit_violation = True
                if state.max_provider_concurrency > GLOBAL_LIMIT:
                    state.unbounded = True
                if time.monotonic() >= next_sample_at:
                    task_ages = [time.monotonic() - started for started in provider.started_calls.values()]
                    rss = process_tree_rss()
                    sample = {
                        "timestamp_utc": utc_now(),
                        "elapsed_seconds": round(state.elapsed(), 3),
                        **rss,
                        "outstanding_async_tasks": sum(not task.done() for task in state.tasks) + broker.active_count,
                        "broker_queued": queued,
                        "broker_running": running,
                        "pipeline_active_per_site": current_pipeline,
                        "pipeline_backlog_per_site": current_backlog,
                        "ipc_depth": ipc_queue.qsize(),
                        "completed": state.completed,
                        "failed": state.failed,
                        "provider_concurrency": provider.active,
                        "provider_concurrency_max": provider.max_active,
                        "hung_task_detected": bool(task_ages and max(task_ages) > HUNG_TASK_SECONDS),
                    }
                    state.samples.append(sample)
                    if sample["hung_task_detected"]:
                        state.hung_task = True
                    state.max_provider_concurrency = max(state.max_provider_concurrency, provider.max_active)
                    persist_checkpoint(state)
                    next_sample_at = time.monotonic() + sample_interval

                # Sleep using real wall clock time and yield to broker workers.
                await asyncio.sleep(max(0.01, min(0.1, 0.1 - (time.monotonic() - loop_started))))
        except MemoryError:
            state.memory_error = True
        except KeyboardInterrupt:
            interrupted = True
            raise
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception:
            state.errors += 1
            raise
        finally:
            interrupted = interrupted or INTERRUPT_REQUESTED
            state.interrupted = interrupted
            await broker.stop(drain=False)
            broker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await broker_task
            state.tasks.discard(broker_task)
            with sessions() as db:
                state.completed = int(db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.status == "completed")) or 0)
                state.failed = int(db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.status == "failed")) or 0)
            engine.dispose()
            persist_checkpoint(state, final=True, interrupted=interrupted)


async def run_harness(duration_seconds: float, artifact_dir: Path, sample_interval: float) -> int:
    state = RunState(duration_seconds, artifact_dir)
    required = duration_seconds >= REQUIRED_SLEEP_PREVENTION_SECONDS
    if state.sleep_prevention["requested"]:
        state.sleep_prevention["active_at_start"] = set_sleep_prevention(enabled=True)
        if required and not state.sleep_prevention["active_at_start"]:
            state.errors += 1

    try:
        await _run_harness(state, duration_seconds, artifact_dir, sample_interval)
    finally:
        state.sleep_prevention["released"] = set_sleep_prevention(enabled=False)
        if required and state.sleep_prevention["requested"] and not state.sleep_prevention["released"]:
            state.errors += 1
        persist_checkpoint(state, final=True, interrupted=state.interrupted or INTERRUPT_REQUESTED)

    return 0 if state.checkpoint(final=True, interrupted=state.interrupted or INTERRUPT_REQUESTED)["pass"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--sample-interval-seconds", type=float, default=SAMPLE_INTERVAL_SECONDS)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args(argv)
    if args.duration_seconds <= 0 or args.sample_interval_seconds <= 0:
        parser.error("duration and sample interval must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    def handle_sigint(_signum: int, _frame: Any) -> None:
        global INTERRUPT_REQUESTED
        INTERRUPT_REQUESTED = True

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        return asyncio.run(run_harness(args.duration_seconds, args.artifact_dir, args.sample_interval_seconds))
    except KeyboardInterrupt:
        # asyncio.run propagates a second/outer cancellation after the async
        # finally path wrote an incomplete summary artifact.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
