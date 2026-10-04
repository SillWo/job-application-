"""Spawn target for one adapter site.

The API process imports this module without importing Playwright/workflow.  A
workflow import happens only inside :func:`worker_entry`, after the child has
been spawned.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import datetime, timezone
from multiprocessing.queues import Queue
from time import monotonic

from .ipc import BoundedChannel, WorkerCommand, WorkerEvent
from .job_object import attach_current_process, close

BROWSER_COMMAND_TIMEOUT = 5.0


async def _handle_open_browser_command(
    command: WorkerCommand,
    events: BoundedChannel,
    *,
    session_id: int,
    generation: int,
    session_factory=None,
) -> None:
    """Run a bounded browser command on the worker-owned page and acknowledge it."""
    from backend.persistence.database import SessionLocal
    from backend.persistence.models import JobSession
    from backend.runtime.lifecycle import cancellation_fence, get_execution
    from backend.schemas.domain import SessionStatus

    factory = session_factory or SessionLocal
    command_id = command.payload.get("command_id", "")
    ok = False
    message = "Запрос открытия браузера устарел"
    authenticated = False
    safe_url = None
    with factory() as db:
        item = db.get(JobSession, session_id)
        execution = get_execution(db, session_id)
        if (
            command.session_id == session_id
            and command.generation == generation
            and item is not None
            and execution is not None
            and execution.generation == generation
            and not cancellation_fence(db, session_id, generation)
            and item.status not in {
                SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED,
                SessionStatus.COMPLETED, SessionStatus.FAILED,
            }
        ):
            try:
                if command.command == "OPEN_BROWSER":
                    from backend.browser.sessions import bring_browser_to_front, get_browser

                    if get_browser(session_id) is None:
                        message = "Браузер сессии ещё не готов"
                    else:
                        await asyncio.wait_for(
                            bring_browser_to_front(session_id), timeout=BROWSER_COMMAND_TIMEOUT,
                        )
                        ok = True
                        message = "Открыто окно браузера сессии"
                elif command.command == "CHECK_LOGIN":
                    from urllib.parse import urlparse, urlunparse

                    from backend.adapters import adapter_registry
                    from backend.browser.sessions import get_browser

                    executor = get_browser(session_id)
                    if executor is None:
                        message = "Браузер сессии ещё не готов"
                    else:
                        login = await asyncio.wait_for(
                            adapter_registry.get(item.adapter_id).get_login_state(executor.page),
                            timeout=BROWSER_COMMAND_TIMEOUT,
                        )
                        authenticated = bool(login.authenticated)
                        message = str(login.message)[:255]
                        raw_url = str(getattr(executor.page, "url", "") or "")
                        parsed = urlparse(raw_url)
                        try:
                            executor.validate_navigation_url(raw_url)
                            safe_url = urlunparse((parsed.scheme.lower(), parsed.netloc, "/", "", "", ""))
                        except (ValueError, TypeError):
                            safe_url = None
                        ok = True
            except TimeoutError:
                message = (
                    "Открытие браузера заняло слишком много времени"
                    if command.command == "OPEN_BROWSER"
                    else "Проверка входа заняла слишком много времени"
                )
            except Exception:
                # Do not expose Playwright internals; the correlated result
                # still lets the UI report a clear, bounded failure.
                message = (
                    "Не удалось открыть окно браузера. Повторите попытку позже."
                    if command.command == "OPEN_BROWSER"
                    else "Не удалось проверить браузер сессии. Повторите попытку позже."
                )
    events.send(WorkerEvent(
        session_id,
        generation,
        "COMMAND_RESULT",
        message=message,
        payload={
            "command_id": command_id,
            "ok": ok,
            "authenticated": authenticated,
            "url": safe_url,
        },
    ))


async def _serve(
    session_id: int,
    generation: int,
    commands: BoundedChannel,
    events: BoundedChannel,
) -> None:
    # Delayed imports are the isolation boundary.  Neither Playwright nor the
    # workflow module is imported by the API process.
    from sqlalchemy import select

    from backend.persistence.database import SessionLocal
    from backend.persistence.models import JobSession, SavedResumeSource, SessionResumeSnapshot
    from backend.runtime.lifecycle import (
        cancellation_fence,
        claim_site_lease,
        ensure_execution,
        get_execution,
        release_site_lease,
        set_stage,
    )
    from backend.schemas.domain import SessionStatus

    terminal_statuses = {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.STOPPED,
        SessionStatus.CANCELLED,
        "CANCELLED",
    }

    def release_site_lease_if_current(db, site_id: str) -> bool:
        execution = get_execution(db, session_id)
        if execution is None or int(execution.generation) != generation:
            return False
        release_site_lease(db, site_id, session_id)
        return True

    def release_site_lease_if_terminal(db, site_id: str) -> bool:
        item = db.get(JobSession, session_id)
        if item is None or item.status not in terminal_statuses:
            return False
        return release_site_lease_if_current(db, site_id)

    def mark_running_if_current(*, allow_paused: bool = False) -> bool:
        """Persist the active workflow stage only for this live generation."""
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            execution = get_execution(db, session_id)
            if (
                item is None
                or execution is None
                or int(execution.generation) != generation
                or cancellation_fence(db, session_id, generation)
                or item.status in terminal_statuses
                or item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED}
                or (item.status == SessionStatus.PAUSED and not allow_paused)
            ):
                db.rollback()
                return False
            execution = set_stage(db, session_id, "RUNNING")
            execution.wait_reason = None
            item.status = SessionStatus.RUNNING
            db.commit()
            return True

    async def prepare_and_run(manager) -> None:
        """Import the immutable resume in this worker, then run the workflow."""
        nonlocal waiting_for_start
        from backend.services.resume_session import (
            ResumeImportError,
            _private_gender_value,
            _redacted_snapshot,
            load_saved_resume_data,
            persist_session_snapshot,
            revalidate_saved_resume_source,
            uses_saved_resume_data,
        )

        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            execution = ensure_execution(db, session_id, stage="PREPARING")
            if item is None:
                return
            # The supervisor generation is the durable late-result fence. A
            # replacement may advance it only after the previous process died.
            if execution.generation > generation:
                return
            was_paused = item.status == SessionStatus.PAUSED
            execution.generation = generation
            execution.worker_pid = os.getpid()
            execution.worker_started_at = datetime.now(timezone.utc)
            db.commit()
            if cancellation_fence(db, session_id, generation):
                return
            existing_snapshot = db.scalar(
                select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
            )
            if existing_snapshot is not None:
                # Recovery resumes the immutable session input; it never
                # re-reads a changed external URL after an API restart.
                try:
                    from backend.schemas.domain import SiteResumeSnapshot

                    full_snapshot = SiteResumeSnapshot.model_validate(existing_snapshot.full_snapshot)
                    public_snapshot, _ = _redacted_snapshot(full_snapshot)
                    if (
                        full_snapshot.source_site != item.adapter_id
                        or full_snapshot.source_url_hash != existing_snapshot.source_url_hash
                        or public_snapshot.content_hash != existing_snapshot.content_hash
                        or SiteResumeSnapshot.model_validate(existing_snapshot.snapshot).content_hash
                        != existing_snapshot.content_hash
                    ):
                        raise ValueError("snapshot integrity mismatch")
                    _private_gender_value(existing_snapshot.private_view)
                except Exception as exc:
                    item.status = SessionStatus.FAILED
                    item.stop_reason = "Неизменяемый снимок резюме повреждён"
                    execution.error = str(exc)[:4000]
                    set_stage(db, session_id, "FAILED")
                    db.commit()
                    return
                if (
                    execution.source_url_hash
                    and existing_snapshot.source_url_hash != execution.source_url_hash
                ) or (
                    execution.source_content_hash
                    and existing_snapshot.content_hash != execution.source_content_hash
                ):
                    item.status = SessionStatus.FAILED
                    item.stop_reason = "Неизменяемый снимок резюме поврежден"
                    set_stage(db, session_id, "FAILED")
                    db.commit()
                    return
                execution = set_stage(db, session_id, "READY")
                item.status = (
                    SessionStatus.PAUSED if was_paused
                    else SessionStatus.RUNNING if execution.start_requested
                    else SessionStatus.PREPARING
                )
                db.commit()
                adapter_id = item.adapter_id
                source = None
            else:
                source = db.scalar(
                    select(SavedResumeSource).where(SavedResumeSource.adapter_id == item.adapter_id)
                )
            if source is None and existing_snapshot is None:
                item.status = SessionStatus.FAILED
                item.stop_reason = "Сохранённый источник резюме не найден"
                set_stage(db, session_id, "FAILED")
                db.commit()
                return
            if source is not None and (
                execution.source_url_hash != getattr(source, "source_url_hash", None)
                or execution.source_url != getattr(source, "source_url", None)
            ):
                item.status = SessionStatus.FAILED
                item.stop_reason = "Источник резюме изменился после создания сессии"
                set_stage(db, session_id, "FAILED")
                db.commit()
                return
            if not claim_site_lease(db, item.adapter_id, session_id, generation):
                item.status = SessionStatus.PREPARING
                execution.wait_reason = "Ожидание освобождения сайта"
                db.commit()
                return
            set_stage(db, session_id, "IMPORTING")
            db.commit()
            adapter_id = item.adapter_id

        if existing_snapshot is not None:
            with SessionLocal() as db:
                execution = set_stage(db, session_id, "READY")
                item = db.get(JobSession, session_id)
                was_paused = item is not None and item.status == SessionStatus.PAUSED
                should_start = bool(execution.start_requested)
                if item is not None:
                    item.status = (
                        SessionStatus.PAUSED if was_paused
                        else SessionStatus.RUNNING if should_start
                        else SessionStatus.PREPARING
                    )
                db.commit()
                if cancellation_fence(db, session_id, generation):
                    return
            if not should_start:
                # Keep this worker as the durable READY owner.  A later
                # /start sends START to it; importing a second snapshot is
                # explicitly forbidden.
                waiting_for_start = True
                return
            if not mark_running_if_current(allow_paused=False):
                return
            try:
                await manager.run(session_id)
            finally:
                with SessionLocal() as db:
                    if release_site_lease_if_terminal(db, adapter_id):
                        db.commit()
            return

        try:
            # The extractor has its own page timeout; this outer budget also
            # bounds adapter code and guarantees worker cleanup.
            async with asyncio.timeout(180):
                with SessionLocal() as db:
                    if uses_saved_resume_data(adapter_id):
                        source = db.scalar(
                            select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id)
                        )
                        if source is None:
                            raise ResumeImportError('Обновите данные резюме во вкладке «Профиль»')
                        refreshed = source
                        snapshot = load_saved_resume_data(source)
                    else:
                        refreshed, snapshot = await revalidate_saved_resume_source(db, adapter_id)
                        if refreshed.status == "unavailable":
                            raise ResumeImportError("Не удалось повторно проверить сохранённое резюме")
                    if cancellation_fence(db, session_id, generation):
                        return
                    accepted_url = execution.source_url
                    accepted_url_hash = execution.source_url_hash
                    imported_url = getattr(refreshed, "source_url", None)
                    if isinstance(imported_url, str):
                        imported_url = imported_url.strip()
                    if (
                        not accepted_url_hash
                        or snapshot.source_url_hash != accepted_url_hash
                        or getattr(refreshed, "source_url_hash", None) != accepted_url_hash
                        or (accepted_url and imported_url != accepted_url)
                    ):
                        raise ResumeImportError("Ссылка источника изменилась во время импорта")
                    expected_content_hash = execution.source_content_hash
                    public_snapshot, _ = _redacted_snapshot(snapshot)
                    if expected_content_hash and public_snapshot.content_hash != expected_content_hash:
                        raise ResumeImportError("Содержимое сохраненного резюме изменилось после создания сессии")
                    persisted = persist_session_snapshot(db, session_id, snapshot)
                    # Cancellation may arrive while persistence is flushing;
                    # do not commit a late import result or move the session
                    # back to READY after STOPPING was durably recorded.
                    if cancellation_fence(db, session_id, generation):
                        db.rollback()
                        return
                    execution = set_stage(db, session_id, "READY")
                    item = db.get(JobSession, session_id)
                    if item is not None:
                        item.status = SessionStatus.RUNNING if execution.start_requested else SessionStatus.PREPARING
                    execution.source_url_hash = snapshot.source_url_hash
                    execution.source_content_hash = persisted.content_hash
                    db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with SessionLocal() as db:
                current_execution = get_execution(db, session_id)
                if current_execution is None or current_execution.generation != generation:
                    db.rollback()
                    return
                item = db.get(JobSession, session_id)
                cancelled = cancellation_fence(db, session_id, generation)
                if item is not None and not cancelled:
                    item.status = SessionStatus.FAILED
                    item.stop_reason = str(exc)[:255]
                    item.finished_at = datetime.now(timezone.utc)
                set_stage(
                    db,
                    session_id,
                    "CANCELLED" if cancelled else "FAILED",
                    wait_reason=("cancelled" if cancelled else str(exc)[:255]),
                )
                execution = current_execution
                execution.error = str(exc)[:4000]
                release_site_lease_if_terminal(db, adapter_id)
                db.commit()
            return
        # The API may set start_requested while the network import is in
        # flight.  Re-read it after import instead of using the detached
        # value captured before the worker yielded.
        with SessionLocal() as db:
            current_execution = get_execution(db, session_id)
            if current_execution is None:
                return
            should_start = bool(current_execution.start_requested)
            cancelled = cancellation_fence(db, session_id, generation)
        if cancelled:
            return
        if not should_start:
            waiting_for_start = True
            return
        if not mark_running_if_current(allow_paused=False):
            return
        try:
            await manager.run(session_id)
        finally:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if item is not None and release_site_lease_if_terminal(db, item.adapter_id):
                    db.commit()

    async def mark_stopped() -> None:
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            execution = get_execution(db, session_id)
            if execution is None or int(execution.generation) != generation:
                db.rollback()
                return
            if item is None:
                db.rollback()
                return
            if item.status == SessionStatus.COMPLETED:
                execution.stage = "COMPLETED"
            elif item.status == SessionStatus.FAILED:
                execution.stage = "FAILED"
            elif item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, "CANCELLED"}:
                execution.stage = "CANCELLED"
            else:
                item.status = SessionStatus.CANCELLED
                item.stop_reason = item.stop_reason or "Остановлено пользователем"
                item.finished_at = datetime.now(timezone.utc)
                execution.stage = "CANCELLED"
                execution.cancel_requested = True
            if item is not None:
                release_site_lease_if_current(db, item.adapter_id)
            db.commit()

    # Importing WorkflowManager is deliberately the first workflow import in
    # the spawned child, after containment and durable PREPARING state exist.
    from backend.orchestrator.workflow import WorkflowManager

    manager = WorkflowManager(generation=generation)
    task: asyncio.Task | None = None
    waiting_for_start = False
    last_heartbeat = 0.0

    def emit(event: WorkerEvent) -> None:
        events.send(event)

    while True:
        now = monotonic()
        if now - last_heartbeat >= 1.0:
            emit(WorkerEvent(session_id, generation, "HEARTBEAT", payload={"pid": os.getpid()}))
            with SessionLocal() as db:
                execution = get_execution(db, session_id)
                if execution is None or int(execution.generation) != generation:
                    db.rollback()
                    return
                execution.heartbeat_at = datetime.now(timezone.utc)
                execution.worker_pid = os.getpid()
                execution.worker_started_at = execution.worker_started_at or datetime.now(timezone.utc)
                db.commit()
                item = db.get(JobSession, session_id)
                if item is not None and item.status not in terminal_statuses:
                    claim_site_lease(db, item.adapter_id, session_id, generation)
                    db.commit()
            last_heartbeat = now
        command = commands.receive()
        if command is None:
            if task is not None and task.done():
                if waiting_for_start:
                    await asyncio.sleep(0.03)
                    continue
                error = None
                if not task.cancelled():
                    error = task.exception()
                with SessionLocal() as db:
                    execution = get_execution(db, session_id)
                    item = db.get(JobSession, session_id)
                    if execution is None or execution.generation != generation:
                        db.rollback()
                        return
                    if item is None:
                        db.rollback()
                        return
                    if error is not None:
                        reason = str(error)[:1000] or "Рабочая задача неожиданно завершилась"
                        if item.status not in terminal_statuses:
                            item.status = SessionStatus.FAILED
                            item.stop_reason = reason[:255]
                            item.finished_at = datetime.now(timezone.utc)
                            execution.stage = "FAILED"
                        execution.error = reason[:4000]
                        event_name = "FAILED"
                        event_message = reason
                    elif item.status == SessionStatus.PAUSED:
                        execution.stage = "PAUSED"
                        execution.wait_reason = item.stop_reason or "Ожидание продолжения сессии"
                        db.commit()
                        waiting_for_start = True
                        emit(WorkerEvent(
                            session_id, generation, "PAUSED", stage="PAUSED",
                            message=execution.wait_reason[:255],
                        ))
                        continue
                    elif item.status == SessionStatus.COMPLETED:
                        execution.stage = "COMPLETED"
                        event_name = "COMPLETED"
                        event_message = ""
                    elif item.status in {
                        SessionStatus.STOPPING, SessionStatus.STOPPED,
                        SessionStatus.CANCELLED, "CANCELLED",
                    }:
                        item.status = SessionStatus.CANCELLED
                        item.finished_at = item.finished_at or datetime.now(timezone.utc)
                        execution.stage = "CANCELLED"
                        execution.cancel_requested = True
                        event_name = "STOPPED"
                        event_message = item.stop_reason or "Сессия остановлена"
                    elif item.status == SessionStatus.FAILED:
                        execution.stage = "FAILED"
                        event_name = "FAILED"
                        event_message = item.stop_reason or execution.error or "Сессия завершилась с ошибкой"
                    else:
                        reason = "Рабочий процесс завершился до окончания сессии"
                        item.status = SessionStatus.FAILED
                        item.stop_reason = reason
                        item.finished_at = datetime.now(timezone.utc)
                        execution.stage = "FAILED"
                        execution.error = "WORKFLOW_RETURNED_NONTERMINAL: Workflow returned before terminal state"
                        event_name = "FAILED"
                        event_message = reason
                    release_site_lease_if_terminal(db, item.adapter_id)
                    db.commit()
                emit(WorkerEvent(session_id, generation, event_name, message=event_message[:1000]))
                return
            await asyncio.sleep(0.03)
            continue
        if not isinstance(command, WorkerCommand) or command.session_id != session_id or command.generation != generation:
            continue
        if command.command in {"START", "RESUME"}:
            if waiting_for_start and task is not None and task.done():
                if not mark_running_if_current(allow_paused=command.command == "RESUME"):
                    continue
                waiting_for_start = False
                task = asyncio.create_task(manager.run(session_id))
                emit(WorkerEvent(session_id, generation, "READY", stage="RUNNING"))
            elif task is None:
                task = asyncio.create_task(prepare_and_run(manager))
                emit(WorkerEvent(session_id, generation, "READY", stage="IMPORTING"))
        elif command.command == "PAUSE":
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            with SessionLocal() as db:
                execution = get_execution(db, session_id)
                item = db.get(JobSession, session_id)
                if execution is None or execution.generation != generation or item is None:
                    db.rollback()
                    return
                if item.status not in terminal_statuses:
                    item.status = SessionStatus.PAUSED
                    item.stop_reason = item.stop_reason or "Сессия приостановлена"
                    execution.stage = "PAUSED"
                    execution.wait_reason = item.stop_reason
                    waiting_for_start = True
                db.commit()
            emit(WorkerEvent(session_id, generation, "PAUSED", stage="PAUSED"))
        elif command.command == "STOP":
            # The durable cancellation fence is written by the API first.
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await mark_stopped()
            emit(WorkerEvent(session_id, generation, "STOPPED", stage="STOPPING"))
            if task is None or task.done():
                return
        elif command.command in {"OPEN_BROWSER", "CHECK_LOGIN"}:
            await _handle_open_browser_command(
                command,
                events,
                session_id=session_id,
                generation=generation,
            )


def worker_entry(session_id: int, generation: int, commands: Queue, events: Queue) -> None:
    """Pickle-safe multiprocessing spawn target."""
    try:
        containment = attach_current_process()
    except Exception as exc:
        # Fail closed before any browser/workflow import. The API receives a
        # bounded event and can mark the durable execution failed.
        BoundedChannel(events, capacity=64).send(
            WorkerEvent(session_id, generation, "FAILED", message=str(exc)[:1000])
        )
        return
    try:
        asyncio.run(_serve(session_id, generation, BoundedChannel(commands, capacity=32), BoundedChannel(events, capacity=64)))
    finally:
        # Closing KILL_ON_JOB_CLOSE also tears down any browser descendants
        # left behind by a crash or an interrupted Playwright shutdown.
        close(containment)
