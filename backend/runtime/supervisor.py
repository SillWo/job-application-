"""API-side supervisor for one bounded, spawn-isolated worker per site."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from .ipc import BoundedChannel, CommandName, WorkerCommand, WorkerEvent
from .job_object import process_identity_matches, terminate_process_tree, wait_descendants_gone
from .worker import worker_entry


@dataclass(slots=True)
class WorkerHandle:
    site_id: str
    session_id: int
    generation: int
    process: Any
    commands: BoundedChannel
    events: BoundedChannel
    last_heartbeat: float = 0.0
    # Retained when a platform can duplicate the worker's containment handle
    # into the supervisor.  Keeping it on the handle makes descendant proof
    # part of replacement, rather than an unrelated PID-only check.
    containment_handle: int | None = None


class RuntimeSupervisor:
    """Owns worker replacement and site exclusivity.

    A replacement is created only after the previous process has joined and
    is dead.  This invariant is kept in one synchronous lock-free API object;
    callers are expected to serialize mutations in the request/event loop.
    """

    def __init__(self, *, queue_size: int = 32, context: str = "spawn", session_factory=None) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.queue_size = queue_size
        self.ctx = mp.get_context(context)
        self._session_factory = session_factory
        self.workers: dict[str, WorkerHandle] = {}
        self.generations: dict[str, int] = {}
        self._command_waiters: dict[str, asyncio.Future] = {}

    def worker(self, site_id: str) -> WorkerHandle | None:
        """Return the registered site owner, including a dead handle awaiting cleanup."""
        handle = self.workers.get(site_id)
        if handle is not None and not handle.process.is_alive() and self._retire(site_id, timeout=0):
            return None
        return handle

    async def browser_command(
        self,
        *,
        site_id: str,
        session_id: int,
        command_name: CommandName,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """Send a correlated browser command and await its bounded worker ack."""
        handle = self.workers.get(site_id)
        if handle is None or handle.session_id != session_id or not handle.process.is_alive():
            return {"ok": False, "message": "Рабочая сессия браузера сейчас недоступна"}
        command_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._command_waiters[command_id] = future
        command = WorkerCommand(
            session_id,
            handle.generation,
            command_name,
            {"command_id": command_id},
        )
        try:
            if not self.send(site_id, command):
                return {"ok": False, "message": "Не удалось передать запрос рабочей сессии"}
            try:
                result = await asyncio.wait_for(future, timeout=max(0.1, timeout))
            except TimeoutError:
                return {"ok": False, "message": "Рабочая сессия не подтвердила открытие браузера вовремя"}
            return result
        finally:
            self._command_waiters.pop(command_id, None)

    async def open_browser(self, *, site_id: str, session_id: int, timeout: float = 8.0) -> dict[str, Any]:
        return await self.browser_command(
            site_id=site_id, session_id=session_id, command_name="OPEN_BROWSER", timeout=timeout,
        )

    async def check_login(self, *, site_id: str, session_id: int, timeout: float = 8.0) -> dict[str, Any]:
        return await self.browser_command(
            site_id=site_id, session_id=session_id, command_name="CHECK_LOGIN", timeout=timeout,
        )

    def _next_generation(self, site_id: str, session_id: int) -> int:
        """Advance the generation in SQL before spawning a replacement."""
        if self._session_factory is None:
            from backend.persistence.database import SessionLocal

            session_factory = SessionLocal
        else:
            session_factory = self._session_factory
        from backend.persistence.execution_models import SessionExecution

        cached = self.generations.get(site_id, 0)
        with session_factory() as db:
            execution = db.scalar(
                select(SessionExecution).where(SessionExecution.session_id == session_id)
            )
            durable = int(execution.generation) if execution is not None else 0
            generation = max(cached, durable) + 1
            if execution is not None:
                execution.generation = generation
                from backend.orchestrator.pipeline import PipelineStore

                if not PipelineStore.adopt_generation(db, session_id, site_id, generation):
                    raise RuntimeError("Could not adopt durable pipeline generation")
            db.commit()
        self.generations[site_id] = generation
        return generation

    def _retire(self, site_id: str, *, timeout: float = 15.0) -> bool:
        handle = self.workers.get(site_id)
        if handle is None:
            return True
        deadline = monotonic() + timeout
        if handle.process.is_alive():
            handle.process.join(max(0.0, deadline - monotonic()))
        if handle.process.is_alive():
            return False
        if not wait_descendants_gone(
            handle.process.pid or 0,
            timeout=max(0.0, deadline - monotonic()),
            handle=handle.containment_handle,
        ):
            return False
        self.workers.pop(site_id, None)
        return True

    def start(self, *, site_id: str, session_id: int) -> WorkerHandle | None:
        current = self.workers.get(site_id)
        if current is not None:
            if current.process.is_alive():
                if current.session_id != session_id:
                    return None
                # This is the /start-after-READY path.  Sending START is
                # idempotent while an import is running and wakes the same
                # worker when it is waiting on auto_start=false.
                current.commands.send(WorkerCommand(session_id, current.generation, "START"))
                return current
            if not self._retire(site_id):
                return None
        generation = self._next_generation(site_id, session_id)
        command_queue = self.ctx.Queue(maxsize=self.queue_size)
        event_queue = self.ctx.Queue(maxsize=self.queue_size * 2)
        process = self.ctx.Process(target=worker_entry, args=(session_id, generation, command_queue, event_queue), daemon=True)
        process.start()
        handle = WorkerHandle(
            site_id, session_id, generation, process,
            BoundedChannel(command_queue, capacity=self.queue_size),
            BoundedChannel(event_queue, capacity=self.queue_size * 2),
            monotonic(),
        )
        self.workers[site_id] = handle
        if not handle.commands.send(WorkerCommand(session_id, generation, "START")):
            self.stop(site_id, session_id=session_id)
            return None
        return handle

    def send(self, site_id: str, command: WorkerCommand) -> bool:
        handle = self.workers.get(site_id)
        if handle is None or handle.session_id != command.session_id or handle.generation != command.generation:
            return False
        return handle.commands.send(command)

    def cancel(self, *, site_id: str, session_id: int, generation: int | None = None) -> bool:
        handle = self.workers.get(site_id)
        if handle is None or handle.session_id != session_id:
            return False
        generation = handle.generation if generation is None else generation
        return self.send(site_id, WorkerCommand(session_id, generation, "STOP"))

    def stop(self, site_id: str, *, session_id: int | None = None, timeout: float = 15.0) -> bool:
        handle = self.workers.get(site_id)
        if handle is None or (session_id is not None and handle.session_id != session_id):
            return True
        deadline = monotonic() + timeout
        self.cancel(site_id=site_id, session_id=handle.session_id)
        handle.process.join(max(0.0, deadline - monotonic()))
        if handle.process.is_alive():
            return False
        if not wait_descendants_gone(
            handle.process.pid or 0,
            timeout=max(0.0, deadline - monotonic()),
            handle=handle.containment_handle,
        ):
            return False
        self.workers.pop(site_id, None)
        return True

    def poll(self, site_id: str) -> list[WorkerEvent]:
        handle = self.workers.get(site_id)
        if handle is None:
            return []
        result: list[WorkerEvent] = []
        while True:
            event = handle.events.receive()
            if event is None:
                break
            if isinstance(event, WorkerEvent) and event.generation == handle.generation:
                result.append(event)
                if event.event == "HEARTBEAT":
                    handle.last_heartbeat = monotonic()
                elif event.event == "COMMAND_RESULT":
                    command_id = event.payload.get("command_id")
                    waiter = self._command_waiters.get(str(command_id))
                    if waiter is not None and not waiter.done():
                        waiter.set_result({
                            "ok": bool(event.payload.get("ok")),
                            "message": event.message[:255],
                            "authenticated": bool(event.payload.get("authenticated")),
                            "url": event.payload.get("url"),
                        })
        return result

    def recover(self) -> list[int]:
        """Resume durable intents after an API restart.

        The old in-process workflow recovery is intentionally absent here;
        every replacement is spawned only after the previous process is dead.
        """
        from backend.adapters import adapter_registry
        from backend.persistence.database import SessionLocal
        from backend.persistence.execution_models import SessionExecution
        from backend.persistence.models import JobSession
        from backend.schemas.domain import SessionStatus

        started: list[int] = []
        with SessionLocal() as db:
            rows = list(db.scalars(select(JobSession).where(
                JobSession.status.in_((SessionStatus.PREPARING, SessionStatus.RUNNING))
            )))
            for item in rows:
                execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == item.id))
                if execution is None or execution.cancel_requested:
                    continue
                # A process that survived an API crash still owns the
                # profile.  Retire it from its durable PID before spawning a
                # replacement; otherwise two Chromium/workflow owners could
                # coexist after restart.
                if execution.worker_pid and not wait_descendants_gone(int(execution.worker_pid), timeout=0):
                    if not process_identity_matches(
                        int(execution.worker_pid), execution.worker_started_at
                    ) or not terminate_process_tree(
                        int(execution.worker_pid), timeout=5.0, started_at=execution.worker_started_at
                    ):
                        # An unproven PID may have been reused.  Do not spawn
                        # a replacement until the old owner is gone.
                        continue
                    execution.worker_pid = None
                    execution.worker_started_at = None
                    db.commit()
                elif execution.worker_pid:
                    execution.worker_pid = None
                    execution.worker_started_at = None
                    db.commit()
                if execution.stage == "READY" and not execution.start_requested:
                    continue
                adapter = adapter_registry.get(item.adapter_id)
                if self.start(site_id=adapter.site_id, session_id=item.id) is not None:
                    started.append(item.id)
        return started

    async def monitor(self, stop_event: asyncio.Event, *, interval: float = 0.2) -> None:
        """Drain bounded IPC and persist worker failures/heartbeats."""
        from backend.adapters import adapter_registry
        from backend.persistence.database import SessionLocal
        from backend.persistence.execution_models import SessionExecution
        from backend.persistence.models import JobSession
        from backend.schemas.domain import SessionStatus

        while not stop_event.is_set():
            for site_id in list(self.workers):
                events = self.poll(site_id)
                for event in events:
                    with SessionLocal() as db:
                        execution = db.scalar(select(SessionExecution).where(
                            SessionExecution.session_id == event.session_id
                        ))
                        if execution is None or event.generation != execution.generation:
                            # A queued event from a dead generation is never
                            # allowed to mutate a newer durable session.
                            continue
                        item = db.get(JobSession, event.session_id)
                        if item is None:
                            continue
                        execution.heartbeat_at = datetime.now(timezone.utc)
                        if event.event == "HEARTBEAT":
                            from backend.runtime.lifecycle import claim_site_lease

                            adapter = adapter_registry.get(item.adapter_id)
                            claim_site_lease(db, adapter.site_id, item.id, event.generation)
                        elif event.event == "READY":
                            # READY is an IPC notification only. The worker
                            # owns the durable lifecycle stage and may already
                            # have advanced it before this queued event arrives.
                            pass
                        elif event.event == "PAUSED":
                            if item.status == SessionStatus.PAUSED:
                                execution.stage = "PAUSED"
                                execution.wait_reason = event.message or item.stop_reason
                            elif item.status not in {
                                SessionStatus.COMPLETED, SessionStatus.STOPPED,
                                SessionStatus.CANCELLED, SessionStatus.FAILED,
                            }:
                                reason = "Рабочий процесс сообщил о паузе, хотя сессия не приостановлена"
                                item.status = SessionStatus.FAILED
                                item.stop_reason = reason
                                item.finished_at = datetime.now(timezone.utc)
                                execution.stage = "FAILED"
                                execution.error = "INVALID_PAUSED_EVENT: worker reported PAUSED while session was active"
                        if event.event == "FAILED":
                            if item is not None and (
                                execution.cancel_requested
                                or item.status in {
                                    SessionStatus.STOPPING,
                                    SessionStatus.STOPPED,
                                    SessionStatus.CANCELLED,
                                }
                            ):
                                item.status = SessionStatus.CANCELLED
                                item.finished_at = datetime.now(timezone.utc)
                                execution.stage = "CANCELLED"
                            elif item.status in {
                                SessionStatus.COMPLETED, SessionStatus.STOPPED,
                                SessionStatus.CANCELLED, "CANCELLED",
                            }:
                                execution.error = event.message[:4000]
                            elif item is not None:
                                item.status = SessionStatus.FAILED
                                item.stop_reason = event.message[:255]
                                item.finished_at = datetime.now(timezone.utc)
                                execution.stage = "FAILED"
                            execution.error = event.message[:4000]
                        elif event.event == "COMPLETED":
                            if item is not None and execution.cancel_requested:
                                item.status = "CANCELLED"
                                execution.stage = "CANCELLED"
                            elif item.status == SessionStatus.COMPLETED:
                                execution.stage = "COMPLETED"
                                item.finished_at = item.finished_at or datetime.now(timezone.utc)
                            elif item.status == SessionStatus.PAUSED:
                                execution.stage = "PAUSED"
                                execution.error = "COMPLETED_EVENT_WHILE_PAUSED: worker reported completion while session was paused"
                            elif item.status not in {
                                SessionStatus.STOPPING, SessionStatus.STOPPED,
                                SessionStatus.CANCELLED, SessionStatus.FAILED,
                            }:
                                reason = "Рабочий процесс завершился до подтверждения окончания сессии"
                                item.status = SessionStatus.FAILED
                                item.stop_reason = reason
                                item.finished_at = datetime.now(timezone.utc)
                                execution.error = "PREMATURE_COMPLETED_EVENT: worker reported completion before durable terminal state"
                                execution.stage = "FAILED"
                            if item.status == SessionStatus.CANCELLED:
                                execution.stage = "CANCELLED"
                            elif item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED}:
                                item.status = SessionStatus.CANCELLED
                                item.finished_at = datetime.now(timezone.utc)
                                execution.stage = "CANCELLED"
                                execution.cancel_requested = True
                            elif item.status == SessionStatus.FAILED:
                                execution.stage = "FAILED"
                            # Preserve an existing durable terminal state; a
                            # spurious worker event cannot rewrite it.
                            if item.status in {
                                SessionStatus.COMPLETED, SessionStatus.CANCELLED,
                                SessionStatus.FAILED,
                            }:
                                item.finished_at = item.finished_at or datetime.now(timezone.utc)
                        elif event.event == "STOPPED":
                            if item.status == SessionStatus.COMPLETED:
                                execution.stage = "COMPLETED"
                                item.finished_at = item.finished_at or datetime.now(timezone.utc)
                            elif item.status == SessionStatus.FAILED:
                                execution.stage = "FAILED"
                                item.finished_at = item.finished_at or datetime.now(timezone.utc)
                            elif item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, "CANCELLED"}:
                                execution.stage = "CANCELLED"
                                item.finished_at = item.finished_at or datetime.now(timezone.utc)
                            else:
                                item.status = "CANCELLED"
                                item.finished_at = datetime.now(timezone.utc)
                                execution.stage = "CANCELLED"
                                execution.cancel_requested = True
                        if event.event in {"FAILED", "COMPLETED", "STOPPED"} and item.status in {
                            SessionStatus.FAILED, SessionStatus.COMPLETED,
                            SessionStatus.STOPPED, SessionStatus.CANCELLED,
                        }:
                            from backend.runtime.lifecycle import release_site_lease

                            release_site_lease(db, adapter_registry.get(item.adapter_id).site_id, item.id)
                            from backend.orchestrator.terminal_finalization import (
                                finalize_terminal_session,
                            )

                            finalize_terminal_session(db, item)
                        db.commit()
                handle = self.workers.get(site_id)
                if handle is not None and not handle.process.is_alive():
                    with SessionLocal() as db:
                        execution = db.scalar(select(SessionExecution).where(
                            SessionExecution.session_id == handle.session_id
                        ))
                        item = db.get(JobSession, handle.session_id)
                        if (
                            execution is None
                            or item is None
                            or execution.generation != handle.generation
                        ):
                            continue
                        terminal_event = any(e.event in {"FAILED", "COMPLETED", "STOPPED"} for e in events)
                        if execution is not None and item is not None and not terminal_event:
                            if execution.cancel_requested or item.status == SessionStatus.STOPPING:
                                item.status = "CANCELLED"
                                execution.stage = "CANCELLED"
                            elif item.status in {
                                SessionStatus.PREPARING,
                                SessionStatus.RUNNING,
                                SessionStatus.PAUSED,
                            }:
                                item.status = SessionStatus.FAILED
                                item.stop_reason = "Рабочий процесс завершился неожиданно"
                                execution.stage = "FAILED"
                                execution.error = (
                                    f"WORKER_PROCESS_EXITED_UNEXPECTEDLY: exitcode={handle.process.exitcode}"
                                )
                            else:
                                execution.stage = execution.stage or "STOPPED"
                            from backend.runtime.lifecycle import release_site_lease

                            if item is not None:
                                release_site_lease(db, adapter_registry.get(item.adapter_id).site_id, item.id)
                            if item.status in {
                                SessionStatus.COMPLETED, SessionStatus.STOPPED,
                                SessionStatus.CANCELLED, SessionStatus.FAILED,
                            }:
                                from backend.orchestrator.terminal_finalization import (
                                    finalize_terminal_session,
                                )

                                finalize_terminal_session(db, item)
                            db.commit()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)

    def close(self) -> None:
        for site_id in list(self.workers):
            self.stop(site_id)
