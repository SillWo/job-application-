"""Durable, bounded orchestration helpers for the vacancy pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.adapters.base.protocol import JobRef
from backend.persistence.execution_models import SessionExecution
from backend.persistence.model_request_models import ModelRequest
from backend.persistence.models import JobSession, Vacancy
from backend.persistence.pipeline_models import (
    PipelineCheckpoint,
    PipelineItem,
    PipelineModelOperation,
)

EVALUATION_QUEUE_LIMIT = 10
_CAPACITY_STAGES = ("discovery", "extraction", "evaluation")
_TERMINAL_VACANCY_STATES = {
    "APPLIED",
    "SUBMITTED",
    "ALREADY_APPLIED",
    "REPORTED",
    "REJECTED_BY_MODEL",
    "REJECTED_BY_RULE",
    "ERROR",
    "CANCELLED",
}
_TERMINAL_SESSION_STATUSES = {"STOPPING", "STOPPED", "COMPLETED", "CANCELLED", "FAILED"}
_STAGE_ORDER = {
    "discovery": 0,
    "extraction": 1,
    "evaluation": 2,
    "letter": 3,
    "submission": 4,
    "reporting": 4,
    "completed": 5,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def current_generation(db: Session, session_id: int, fallback: int | None = None) -> int:
    execution = db.scalar(
        select(SessionExecution).where(SessionExecution.session_id == session_id)
    )
    if execution is None:
        return max(0, int(fallback)) if fallback is not None else 0
    actual = max(0, int(execution.generation))
    if fallback is not None and int(fallback) != actual:
        return -1
    return actual


class PipelineStore:
    """Small transactional API shared by workers and integration tests."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def adopt_generation(
        db: Session, session_id: int, site_id: str, new_generation: int
    ) -> bool:
        """Adopt pending work after takeover; the caller owns the transaction.

        The supervisor updates SessionExecution.generation first, invokes this
        helper, then commits both changes together. Old model requests stay in
        their original generation as cancelled audit records; active links are
        retired so the new worker creates its own requests.
        """
        # The supervisor may have incremented generation inside this same
        # transaction, and session factories commonly disable autoflush.
        db.flush()
        execution = db.scalar(
            select(SessionExecution).where(SessionExecution.session_id == session_id)
        )
        owner = db.get(JobSession, session_id)
        if (
            owner is None
            or owner.status in _TERMINAL_SESSION_STATUSES
            or execution is None
            or int(execution.generation) != int(new_generation)
            or execution.cancel_requested
            or new_generation < 0
        ):
            return False

        prior_generations = list(db.scalars(select(func.distinct(PipelineItem.generation)).where(
            PipelineItem.session_id == session_id,
            PipelineItem.site_id == site_id,
            PipelineItem.generation < new_generation,
        ).order_by(PipelineItem.generation.asc())))
        prior_checkpoint_rows = list(db.scalars(select(PipelineCheckpoint).where(
            PipelineCheckpoint.session_id == session_id,
            PipelineCheckpoint.site_id == site_id,
            PipelineCheckpoint.name == "discovery",
            PipelineCheckpoint.generation < new_generation,
        ).order_by(PipelineCheckpoint.generation.asc())))
        for old_generation in prior_generations:
            PipelineStore._reconcile(db, session_id, site_id, int(old_generation))

        now = utcnow()
        db.execute(
            update(ModelRequest)
            .where(
                ModelRequest.session_id == session_id,
                ModelRequest.site_id == site_id,
                ModelRequest.generation < new_generation,
                ModelRequest.status.in_(("queued", "running", "retry")),
            )
            .values(
                status="cancelled",
                completed_at=now,
                lease_owner=None,
                heartbeat_at=None,
                error_code="cancelled",
            )
            .execution_options(synchronize_session=False)
        )
        old_rows = list(db.scalars(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.site_id == site_id,
            PipelineItem.generation < new_generation,
        ).order_by(PipelineItem.created_at.asc(), PipelineItem.id.asc())))
        target_rows = list(db.scalars(select(PipelineItem).where(
            PipelineItem.session_id == session_id,
            PipelineItem.site_id == site_id,
            PipelineItem.generation == new_generation,
        ).order_by(PipelineItem.created_at.asc(), PipelineItem.id.asc())))
        target_by_external_id = {row.external_id: row for row in target_rows}
        terminal_ids = {
            row.external_id
            for row in [*old_rows, *target_rows]
            if row.status in {"completed", "failed", "cancelled"} or row.stage == "completed"
        }
        active_ids: set[str] = set()

        for old in old_rows:
            if old.status not in {"queued", "running"} or old.stage == "completed":
                continue
            target = target_by_external_id.get(old.external_id)
            if target is not None:
                if target.status in {"completed", "failed", "cancelled"} or target.stage == "completed":
                    old.status = "cancelled"
                    old.error_code = "superseded_generation"
                    old.request_id = None
                    old.diagnostic_id = None
                    old.updated_at = now
                    terminal_ids.add(old.external_id)
                    continue
                if _STAGE_ORDER[old.stage] > _STAGE_ORDER[target.stage]:
                    target.stage = old.stage
                    target.stage_revision = max(target.stage_revision, old.stage_revision)
                elif old.stage == target.stage:
                    target.stage_revision = max(target.stage_revision, old.stage_revision)
                target.status = (
                    "running" if "running" in {old.status, target.status} else "queued"
                )
                target.created_at = min(target.created_at, old.created_at)
                target.updated_at = now
                # The target generation may own a valid current request link;
                # leave it intact. The older duplicate row is retired.
                old.status = "cancelled"
                old.error_code = "superseded_generation"
                old.request_id = None
                old.diagnostic_id = None
                old.updated_at = now
                active_ids.add(target.external_id)
                continue

            old.generation = new_generation
            old.request_id = None
            old.diagnostic_id = None
            old.updated_at = now
            active_ids.add(old.external_id)
            target_by_external_id[old.external_id] = old

        operations = list(db.scalars(select(PipelineModelOperation).where(
            PipelineModelOperation.session_id == session_id,
            PipelineModelOperation.site_id == site_id,
            PipelineModelOperation.generation < new_generation,
            PipelineModelOperation.status.in_(
                ("submitting", "queued", "running", "retry")
            ),
        )))
        for operation in operations:
            operation.status = "cancelled"
            operation.request_id = None
            operation.diagnostic_id = None
            operation.updated_at = now

        target_checkpoint = PipelineStore._checkpoint(db, session_id, site_id, new_generation)
        target_data = dict(target_checkpoint.data or {})
        source_rows = [*prior_checkpoint_rows, target_checkpoint]
        merged_data: dict[str, Any] = {}
        merged_backlog: dict[str, dict[str, Any]] = {}
        merged_urls: dict[str, Any] = {}
        high_water = int(target_data.get("high_water", 0) or 0)
        for checkpoint in source_rows:
            data = dict(checkpoint.data or {})
            merged_data.update(data)
            high_water = max(high_water, int(data.get("high_water", 0) or 0))
            urls = data.get("urls", {})
            if isinstance(urls, dict):
                merged_urls.update(urls)
            backlog = data.get("backlog", [])
            if isinstance(backlog, list):
                for raw in backlog:
                    try:
                        candidate = JobRef.model_validate(raw)
                    except (TypeError, ValueError):
                        continue
                    merged_backlog.setdefault(
                        candidate.external_id, candidate.model_dump(mode="json")
                    )

        vacancy_urls = {
            row.external_id: row.url
            for row in db.scalars(select(Vacancy).where(
                Vacancy.session_id == session_id,
                Vacancy.source == site_id,
            ))
            if row.external_id and row.url
        }
        for row in [*old_rows, *target_rows]:
            # PipelineItem stores identity and stage only. URLs are retained
            # in the discovery checkpoint, with the vacancy row as fallback.
            if row.external_id in active_ids:
                merged_urls.setdefault(row.external_id, vacancy_urls.get(row.external_id))
            if row.status in {"completed", "failed", "cancelled"} or row.stage == "completed":
                terminal_ids.add(row.external_id)
        for external_id in [*merged_backlog]:
            if external_id in active_ids or external_id in terminal_ids:
                merged_backlog.pop(external_id, None)
        target_checkpoint.data = {
            **merged_data,
            "backlog": list(merged_backlog.values()),
            "high_water": high_water,
            "queue_limit": EVALUATION_QUEUE_LIMIT,
            "urls": merged_urls,
        }
        target_checkpoint.revision += 1
        target_checkpoint.updated_at = now
        return True

    @staticmethod
    def _checkpoint(db: Session, session_id: int, site_id: str, generation: int) -> PipelineCheckpoint:
        row = db.scalar(
            select(PipelineCheckpoint).where(
                PipelineCheckpoint.session_id == session_id,
                PipelineCheckpoint.site_id == site_id,
                PipelineCheckpoint.name == "discovery",
                PipelineCheckpoint.generation == generation,
            )
        )
        if row is None:
            row = PipelineCheckpoint(
                session_id=session_id,
                site_id=site_id,
                name="discovery",
                generation=generation,
                data={"backlog": [], "high_water": 0},
                updated_at=utcnow(),
            )
            db.add(row)
            db.flush()
        return row

    @staticmethod
    def _reconcile(db: Session, session_id: int, site_id: str, generation: int) -> None:
        rows = db.execute(
            select(PipelineItem, Vacancy)
            .outerjoin(Vacancy, Vacancy.id == PipelineItem.vacancy_id)
            .where(
                PipelineItem.session_id == session_id,
                PipelineItem.site_id == site_id,
                PipelineItem.generation == generation,
                PipelineItem.status.in_(("queued", "running")),
            )
        ).all()
        now = utcnow()
        for pipeline_item, vacancy in rows:
            if vacancy is None:
                vacancy = db.scalar(
                    select(Vacancy).where(
                        Vacancy.session_id == session_id,
                        Vacancy.source == site_id,
                        Vacancy.external_id == pipeline_item.external_id,
                    )
                )
                if vacancy is not None:
                    pipeline_item.vacancy_id = vacancy.id
            if vacancy is None:
                continue
            if vacancy.state in _TERMINAL_VACANCY_STATES:
                pipeline_item.stage = "completed"
                pipeline_item.status = "completed"
                pipeline_item.updated_at = now
            elif vacancy.state == "EXTRACTED":
                PipelineStore._advance_row(pipeline_item, "extraction", now)
            elif vacancy.state == "EVALUATING":
                PipelineStore._advance_row(pipeline_item, "evaluation", now)
            elif vacancy.state == "READY_TO_SUBMIT" or vacancy.state == "SUBMITTING":
                PipelineStore._advance_row(pipeline_item, "submission", now)
            elif vacancy.state == "READY_TO_REPORT":
                PipelineStore._advance_row(pipeline_item, "reporting", now)

    @staticmethod
    def _advance_row(row: PipelineItem, stage: str, now: datetime) -> bool:
        if row.status in {"completed", "failed", "cancelled"}:
            return False
        if _STAGE_ORDER[stage] < _STAGE_ORDER[row.stage]:
            return False
        if row.stage != stage:
            row.stage = stage
            row.stage_revision += 1
        if stage == "completed":
            row.status = "completed"
        else:
            row.status = "running" if stage != "discovery" else "queued"
        row.updated_at = now
        return True

    def enqueue(
        self,
        session_id: int,
        site_id: str,
        refs: Iterable[JobRef | dict[str, Any]],
        *,
        generation: int | None = None,
    ) -> list[JobRef]:
        """Queue at most ten evaluation candidates per site and durably backlog the rest."""
        normalized = [
            ref if isinstance(ref, JobRef) else JobRef.model_validate(ref)
            for ref in refs
        ]
        with self.session_factory() as db:
            gen = current_generation(db, session_id, generation)
            if gen < 0:
                return []
            owner = db.get(JobSession, session_id)
            if owner is None or owner.status in _TERMINAL_SESSION_STATUSES:
                return []
            self._reconcile(db, session_id, site_id, gen)
            db.flush()
            checkpoint = self._checkpoint(db, session_id, site_id, gen)
            data = dict(checkpoint.data or {})
            backlog_by_id: dict[str, dict[str, Any]] = {}
            for raw in data.get("backlog", []):
                try:
                    candidate = JobRef.model_validate(raw)
                except (TypeError, ValueError):
                    continue
                backlog_by_id[candidate.external_id] = candidate.model_dump(mode="json")
            for candidate in normalized:
                backlog_by_id[candidate.external_id] = candidate.model_dump(mode="json")

            existing = list(db.scalars(select(PipelineItem).where(
                PipelineItem.session_id == session_id,
                PipelineItem.site_id == site_id,
                PipelineItem.generation == gen,
            )))
            existing_ids = {row.external_id for row in existing}
            active_site = int(db.scalar(
                select(func.count(PipelineItem.id))
                .select_from(PipelineItem)
                .join(JobSession, JobSession.id == PipelineItem.session_id)
                .outerjoin(SessionExecution, SessionExecution.session_id == JobSession.id)
                .where(
                    PipelineItem.site_id == site_id,
                    PipelineItem.stage.in_(_CAPACITY_STAGES),
                    PipelineItem.status.in_(("queued", "running")),
                    JobSession.status.not_in(_TERMINAL_SESSION_STATUSES),
                    or_(SessionExecution.id.is_(None), PipelineItem.generation == SessionExecution.generation),
                )
            ) or 0)
            slots = max(0, EVALUATION_QUEUE_LIMIT - active_site)
            for external_id in list(backlog_by_id):
                if external_id in existing_ids:
                    backlog_by_id.pop(external_id, None)
                    continue
                if slots <= 0:
                    continue
                db.add(PipelineItem(
                    session_id=session_id,
                    site_id=site_id,
                    external_id=external_id,
                    stage="discovery",
                    status="queued",
                    generation=gen,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                ))
                existing_ids.add(external_id)
                backlog_by_id.pop(external_id, None)
                slots -= 1
                active_site += 1

            high_water = max(int(data.get("high_water", 0) or 0), active_site)
            url_map = dict(data.get("urls", {}))
            url_map.update({candidate.external_id: candidate.url for candidate in normalized})
            checkpoint.data = {
                **data,
                "backlog": list(backlog_by_id.values()),
                "high_water": high_water,
                "queue_limit": EVALUATION_QUEUE_LIMIT,
                "urls": url_map,
            }
            checkpoint.revision += 1
            checkpoint.updated_at = utcnow()
            db.commit()

            active = list(db.scalars(
                select(PipelineItem).where(
                    PipelineItem.session_id == session_id,
                    PipelineItem.site_id == site_id,
                    PipelineItem.generation == gen,
                    PipelineItem.status.in_(("queued", "running")),
                ).order_by(PipelineItem.created_at.asc(), PipelineItem.id.asc())
            ))
            vacancy_urls = {
                row.external_id: row.url
                for row in db.scalars(select(Vacancy).where(Vacancy.session_id == session_id))
                if row.external_id
            }
            input_urls = {candidate.external_id: candidate.url for candidate in normalized}
            result: list[JobRef] = []
            for row in active:
                url = (
                    input_urls.get(row.external_id)
                    or url_map.get(row.external_id)
                    or vacancy_urls.get(row.external_id)
                )
                if not url:
                    # Never hand a fabricated destination to a browser
                    # adapter.  A damaged historical checkpoint is terminal
                    # for this item and frees capacity for valid backlog work.
                    row.status = "failed"
                    row.error_code = "missing_source_url"
                    row.updated_at = utcnow()
                    continue
                result.append(JobRef(external_id=row.external_id, url=url))
            db.commit()
            return result

    def advance(
        self,
        session_id: int,
        site_id: str,
        external_id: str,
        stage: str,
        *,
        generation: int | None = None,
        vacancy_id: int | None = None,
    ) -> bool:
        if stage not in _STAGE_ORDER:
            raise ValueError(f"Unknown pipeline stage: {stage}")
        with self.session_factory() as db:
            gen = current_generation(db, session_id, generation)
            if gen < 0:
                return False
            owner = db.get(JobSession, session_id)
            if owner is None or owner.status in _TERMINAL_SESSION_STATUSES:
                return False
            row = db.scalar(select(PipelineItem).where(
                PipelineItem.session_id == session_id,
                PipelineItem.site_id == site_id,
                PipelineItem.external_id == external_id,
                PipelineItem.generation == gen,
            ))
            if row is None:
                return False
            if vacancy_id is not None:
                row.vacancy_id = vacancy_id
            changed = self._advance_row(row, stage, utcnow())
            db.commit()
            return changed

    def cancel_session(self, session_id: int, *, generation: int | None = None) -> list[str]:
        """Fence pipeline progress and return broker request ids to cancel."""
        with self.session_factory() as db:
            gen = current_generation(db, session_id, generation)
            rows = list(db.scalars(select(PipelineItem).where(
                PipelineItem.session_id == session_id,
                PipelineItem.generation == gen,
                PipelineItem.status.in_(("queued", "running")),
            )))
            request_ids = [row.request_id for row in rows if row.request_id]
            operations = list(db.scalars(select(PipelineModelOperation).where(
                PipelineModelOperation.session_id == session_id,
                PipelineModelOperation.generation == gen,
                PipelineModelOperation.status.in_(("submitting", "queued", "running", "retry")),
            )))
            request_ids.extend(row.request_id for row in operations if row.request_id)
            now = utcnow()
            for row in rows:
                row.status = "cancelled"
                row.error_code = "cancelled"
                row.updated_at = now
            for operation in operations:
                operation.status = "cancelled"
                operation.updated_at = now
            db.commit()
            return list(dict.fromkeys(request_ids))

    def queue_metrics(self, site_id: str, *, generation: int | None = None) -> dict[str, int]:
        with self.session_factory() as db:
            clauses = [
                PipelineItem.site_id == site_id,
                PipelineItem.stage.in_(_CAPACITY_STAGES),
                PipelineItem.status.in_(("queued", "running")),
                JobSession.status.not_in(_TERMINAL_SESSION_STATUSES),
                or_(SessionExecution.id.is_(None), PipelineItem.generation == SessionExecution.generation),
            ]
            if generation is not None:
                clauses.append(PipelineItem.generation == generation)
            active = int(db.scalar(
                select(func.count(PipelineItem.id))
                .select_from(PipelineItem)
                .join(JobSession, JobSession.id == PipelineItem.session_id)
                .outerjoin(SessionExecution, SessionExecution.session_id == JobSession.id)
                .where(*clauses)
            ) or 0)
            high_water = max(
                [
                    int((row.data or {}).get("high_water", 0) or 0)
                    for row in db.scalars(select(PipelineCheckpoint).where(
                        PipelineCheckpoint.site_id == site_id,
                        PipelineCheckpoint.name == "discovery",
                    ))
                ]
                or [0]
            )
            return {"active": active, "high_water": high_water, "limit": EVALUATION_QUEUE_LIMIT}


class DurablePipelineCoordinator(PipelineStore):
    """Runtime hooks preserving browser serialization without model serialization.

    The durable site lease is the cross-process authority.  This local lock is
    an additional invariant for composed/in-process adapters and acceptance
    harnesses: browser operations for a site never overlap, while model waits
    intentionally do not take it and can use all broker capacity.
    """

    _site_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def _site_lock(cls, site_id: str) -> asyncio.Lock:
        lock = cls._site_locks.get(site_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._site_locks[site_id] = lock
        return lock

    async def browser_operation(self, site_id: str, operation):
        async with self._site_lock(site_id):
            value = operation()
            return await value if hasattr(value, "__await__") else value

    @staticmethod
    async def model_operation(operation):
        value = operation()
        return await value if hasattr(value, "__await__") else value
