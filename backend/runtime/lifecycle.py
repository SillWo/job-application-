"""Small transactional helpers shared by API and the process supervisor."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.persistence.execution_models import (
    SessionExecution,
    SessionIdempotencyKey,
    SiteExecutionLease,
)
from backend.persistence.models import JobSession
from backend.schemas.domain import SessionStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_payload_hash(payload: Any) -> str:
    """Hash the validated request shape, independent of JSON key ordering."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_execution(db: Session, session_id: int) -> SessionExecution | None:
    return db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))


def ensure_execution(
    db: Session,
    session_id: int,
    *,
    stage: str = "PREPARING",
    source_url: str | None = None,
    source_url_hash: str | None = None,
    source_content_hash: str | None = None,
) -> SessionExecution:
    execution = get_execution(db, session_id)
    if execution is None:
        now = utcnow()
        execution = SessionExecution(
            session_id=session_id,
            stage=stage,
            stage_started_at=now,
            last_progress_at=now,
            source_url=source_url,
            source_url_hash=source_url_hash,
            source_content_hash=source_content_hash,
        )
        db.add(execution)
        db.flush()
    return execution


def set_stage(
    db: Session,
    session_id: int,
    stage: str,
    *,
    wait_reason: str | None = None,
    next_retry_at: datetime | None = None,
) -> SessionExecution:
    execution = ensure_execution(db, session_id, stage=stage)
    now = utcnow()
    if execution.stage != stage:
        execution.stage_started_at = now
    execution.stage = stage
    execution.last_progress_at = now
    execution.wait_reason = wait_reason
    execution.next_retry_at = next_retry_at
    return execution


def request_start(db: Session, session_id: int) -> SessionExecution:
    execution = ensure_execution(db, session_id)
    execution.start_requested = True
    execution.last_progress_at = utcnow()
    return execution


def request_cancel(db: Session, session_id: int, *, reason: str = "user") -> SessionExecution | None:
    item = db.get(JobSession, session_id)
    execution = ensure_execution(db, session_id, stage="STOPPING")
    # ``ensure_execution`` intentionally does not overwrite an existing
    # stage.  Cancellation is different: STOPPING is the durable fence that
    # must be visible before IPC is attempted, even when an import/workflow
    # already owns the execution row.
    now = utcnow()
    if execution.stage != "STOPPING":
        execution.stage_started_at = now
    execution.stage = "STOPPING"
    execution.cancel_requested = True
    execution.last_progress_at = now
    execution.wait_reason = reason[:255]
    if item is not None and item.status not in {
        SessionStatus.STOPPED, SessionStatus.COMPLETED, SessionStatus.FAILED,
    }:
        item.status = SessionStatus.STOPPING
        item.stop_reason = reason[:255]
    return execution


def cancellation_fence(db: Session, session_id: int, generation: int | None = None) -> bool:
    item = db.get(JobSession, session_id)
    execution = get_execution(db, session_id)
    if item is None:
        return True
    # Legacy/unit workflow callers may not have the runtime execution row.
    # They are outside the process-isolated runtime and should retain the old
    # behavior; every real worker creates this row before doing browser work.
    if execution is None:
        return item.status in {
            SessionStatus.STOPPING,
            SessionStatus.STOPPED,
            "CANCELLED",
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        }
    if item.status in {
        SessionStatus.STOPPING,
        SessionStatus.STOPPED,
        "CANCELLED",
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
    }:
        return True
    return bool(execution.cancel_requested or (generation is not None and execution.generation != generation))


def idempotency_record(db: Session, key: str, payload_hash: str, session_id: int, *, scope: str = "sessions") -> SessionIdempotencyKey:
    row = SessionIdempotencyKey(scope=scope, idempotency_key=key, payload_hash=payload_hash, session_id=session_id)
    db.add(row)
    db.flush()
    return row


def find_idempotency(db: Session, key: str, *, scope: str = "sessions") -> SessionIdempotencyKey | None:
    return db.scalar(select(SessionIdempotencyKey).where(
        SessionIdempotencyKey.scope == scope,
        SessionIdempotencyKey.idempotency_key == key,
    ))


def claim_site_lease(db: Session, site_id: str, session_id: int, generation: int) -> bool:
    current = db.get(SiteExecutionLease, site_id)
    if current is not None and current.session_id != session_id:
        return False
    # A late worker must never move a durable lease backwards.  The API and
    # worker both use this helper, so this is the common fencing point for
    # preview/import/workflow ownership.
    if current is not None and generation < current.generation:
        return False
    if current is None:
        db.add(SiteExecutionLease(site_id=site_id, session_id=session_id, generation=generation, acquired_at=utcnow()))
    else:
        current.generation = generation
        current.heartbeat_at = utcnow()
    return True


def release_site_lease(db: Session, site_id: str, session_id: int) -> bool:
    current = db.get(SiteExecutionLease, site_id)
    if current is None or current.session_id != session_id:
        return False
    db.delete(current)
    return True
