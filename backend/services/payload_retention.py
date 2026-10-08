"""Bounded retention for durable model prompts and cached responses."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.orm import Session

from backend.persistence.model_request_models import ModelRequest, ModelResponseCache
from backend.persistence.models import Application, JobSession, Vacancy
from backend.persistence.pipeline_models import (
    PipelineCheckpoint,
    PipelineItem,
    PipelineModelOperation,
)

RETENTION_DAYS = 30
RETENTION_BATCH_SIZE = 100
RETENTION_SESSION_SCAN_LIMIT = 1000
TERMINAL_SESSION_STATUSES = ("COMPLETED", "STOPPED", "FAILED", "CANCELLED")
TERMINAL_REQUEST_STATUSES = ("completed", "failed", "cancelled")
PENDING_MODEL_STATUSES = ("queued", "running", "retry")
PENDING_OPERATION_STATUSES = ("submitting", "queued", "running", "retry")
UNCERTAIN_SUBMISSION_STATES = ("PARTIAL", "SUBMITTING", "UNCONFIRMED")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _contains_uncertain_submission(value: Any, *, in_submission: bool = False) -> bool:
    if isinstance(value, dict):
        if bool(value.get("cv_confirmed")) and bool(value.get("cover_letter_pending")):
            return True
        for raw_key, child in value.items():
            key = str(raw_key).casefold().replace("-", "_")
            submission_context = in_submission or "submission" in key or "submit" in key
            if key == "submission_reconciliation_attempts":
                try:
                    if int(child) > 0:
                        return True
                except (TypeError, ValueError):
                    return True
            if (
                submission_context
                and key in {"status", "state", "result"}
                and isinstance(child, str)
                and child.casefold() in {
                    "unknown", "uncertain", "pending", "submitting", "partial", "unconfirmed",
                }
            ):
                return True
            if _contains_uncertain_submission(child, in_submission=submission_context):
                return True
    elif isinstance(value, list):
        return any(_contains_uncertain_submission(item, in_submission=in_submission) for item in value)
    return False


def _session_is_protected(db: Session, item: JobSession) -> bool:
    recovery = item.recovery if isinstance(item.recovery, dict) else {}
    if recovery.get("recovering") is True or recovery.get("recovery_pending") is True:
        return True
    recovery_state = str(recovery.get("state", recovery.get("status", ""))).casefold()
    if recovery_state in {"recovering", "resuming", "recovery_pending"}:
        return True
    if _contains_uncertain_submission(item.recovery):
        return True
    if db.scalar(select(ModelRequest.id).where(
        ModelRequest.session_id == item.id,
        ModelRequest.status.in_(PENDING_MODEL_STATUSES),
    ).limit(1)) is not None:
        return True
    if db.scalar(select(PipelineModelOperation.id).where(
        PipelineModelOperation.session_id == item.id,
        PipelineModelOperation.status.in_(PENDING_OPERATION_STATUSES),
    ).limit(1)) is not None:
        return True
    if db.scalar(select(PipelineItem.id).where(
        PipelineItem.session_id == item.id,
        PipelineItem.stage.in_(("letter", "submission")),
        PipelineItem.status.in_(("queued", "running")),
    ).limit(1)) is not None:
        return True
    legacy_partial_progress = and_(
        Vacancy.state == "ERROR",
        Vacancy.data["submission_progress"]["cv_confirmed"].as_boolean().is_(True),
        Vacancy.data["submission_progress"]["cover_letter_pending"].as_boolean().is_(True),
    )
    if db.scalar(select(Vacancy.id).where(
        Vacancy.session_id == item.id,
        or_(
            Vacancy.state.in_(UNCERTAIN_SUBMISSION_STATES),
            Vacancy.error_code == "SUBMISSION_UNCONFIRMED",
            legacy_partial_progress,
        ),
    ).limit(1)) is not None:
        return True
    if db.scalar(select(Application.id).join(Vacancy, Application.vacancy_id == Vacancy.id).where(
        Vacancy.session_id == item.id,
        func.lower(Application.status).in_(("unknown", "uncertain", "pending", "submitting")),
    ).limit(1)) is not None:
        return True
    if db.scalar(select(PipelineCheckpoint.id).where(
        PipelineCheckpoint.session_id == item.id,
        or_(
            func.lower(PipelineCheckpoint.name).like("%submit%"),
            func.lower(PipelineCheckpoint.name).like("%application%"),
        ),
    ).limit(1)) is not None:
        return True
    checkpoints = db.scalars(select(PipelineCheckpoint.data).where(
        PipelineCheckpoint.session_id == item.id,
    )).all()
    return any(_contains_uncertain_submission(checkpoint) for checkpoint in checkpoints)


def prune_payloads(
    db: Session,
    *,
    now: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
    batch_size: int = RETENTION_BATCH_SIZE,
    after_session_id: int = 0,
) -> dict[str, int]:
    """Clear at most ``batch_size`` model payloads and delete that many cache rows."""
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    if not 1 <= batch_size <= RETENTION_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {RETENTION_BATCH_SIZE}")
    if after_session_id < 0:
        raise ValueError("after_session_id must be non-negative")
    current = now or datetime.now(timezone.utc)
    cutoff = _aware(current) - timedelta(days=retention_days)
    candidates = list(db.scalars(
        select(JobSession)
        .where(
            JobSession.status.in_(TERMINAL_SESSION_STATUSES),
            JobSession.finished_at.is_not(None),
            JobSession.finished_at <= cutoff,
            JobSession.id > after_session_id,
        )
        .order_by(JobSession.id)
        .limit(RETENTION_SESSION_SCAN_LIMIT)
    ))
    next_session_id = candidates[-1].id if candidates else 0
    eligible_ids = [
        item.id for item in candidates if not _session_is_protected(db, item)
    ][:batch_size]
    if not eligible_ids:
        return {
            "sessions_checked": len(candidates),
            "cache_rows_deleted": 0,
            "requests_cleared": 0,
            "next_session_id": next_session_id,
        }

    cache_ids = list(db.scalars(
        select(ModelResponseCache.id)
        .where(
            ModelResponseCache.session_id.in_(eligible_ids),
            ModelResponseCache.created_at <= cutoff,
        )
        .order_by(ModelResponseCache.id)
        .limit(batch_size)
    ))
    if cache_ids:
        db.query(ModelResponseCache).filter(ModelResponseCache.id.in_(cache_ids)).delete(
            synchronize_session=False
        )

    operation_for_request = select(PipelineModelOperation.id).where(
        PipelineModelOperation.request_id == ModelRequest.id,
    )
    completed_operation_for_request = select(PipelineModelOperation.id).where(
        PipelineModelOperation.request_id == ModelRequest.id,
        PipelineModelOperation.status == "completed",
    )
    rows = list(db.scalars(
        select(ModelRequest)
        .where(
            ModelRequest.session_id.in_(eligible_ids),
            ModelRequest.status.in_(TERMINAL_REQUEST_STATUSES),
            ModelRequest.created_at <= cutoff,
            or_(
                ModelRequest.canonical_input != "{}",
                and_(
                    ModelRequest.canonical_output.is_not(None),
                    ModelRequest.canonical_output != "{}",
                ),
            ),
            or_(
                ModelRequest.status != "completed",
                ~exists(operation_for_request),
                exists(completed_operation_for_request),
            ),
        )
        .order_by(ModelRequest.id)
        .limit(batch_size)
    ))
    request_ids = [row.id for row in rows]
    if request_ids:
        db.execute(
            update(ModelRequest)
            .where(ModelRequest.id.in_(request_ids))
            .values(
                canonical_input="{}",
                canonical_output=case(
                    (ModelRequest.status == "completed", "{}"),
                    else_=None,
                ),
                error_code=case(
                    (ModelRequest.status == "completed", "payload_retained_metadata"),
                    else_=ModelRequest.error_code,
                ),
            )
            .execution_options(synchronize_session=False)
        )
    return {
        "sessions_checked": len(candidates),
        "cache_rows_deleted": len(cache_ids),
        "requests_cleared": len(request_ids),
        "next_session_id": next_session_id,
    }


async def run_retention_maintenance(
    stop: asyncio.Event,
    *,
    interval_seconds: float = 10 * 60,
) -> None:
    """Run bounded startup/daily passes and stop promptly during app shutdown."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    from backend.persistence.database import SessionLocal

    logger = logging.getLogger(__name__)
    after_session_id = 0
    while not stop.is_set():
        def one_pass() -> None:
            nonlocal after_session_id
            try:
                with SessionLocal() as db:
                    result = prune_payloads(db, after_session_id=after_session_id)
                    db.commit()
                    after_session_id = result["next_session_id"]
            except Exception as exc:
                logger.warning("Payload retention pass failed (%s)", type(exc).__name__)

        await asyncio.to_thread(one_pass)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
