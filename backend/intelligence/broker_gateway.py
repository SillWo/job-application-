"""Workflow-side gateway that can only communicate through the DB broker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.config import settings
from backend.intelligence.gateway import (
    ModelOverloaded,
    ModelPermanentError,
    ModelTimeout,
    ModelUnavailable,
)
from backend.intelligence.letter_claims import LETTER_CLAIMS_VERSION
from backend.intelligence.model_broker import (
    ModelRequestClient,
    ModelVersions,
    SubmitRequest,
    _durable_attempt_expression,
)
from backend.intelligence.security import PromptInjectionDetected, sanitize_untrusted_input
from backend.orchestrator.search_version import HIREHI_SEARCH_ADAPTIVE_V3
from backend.persistence.execution_models import SessionExecution
from backend.persistence.model_request_models import ModelRequest
from backend.persistence.models import AIModelSettings, JobSession
from backend.persistence.pipeline_models import PipelineItem, PipelineModelOperation

T = TypeVar("T", bound=BaseModel)

BROKER_PROMPT_VERSION = "workflow-prompts-2026-10-08-required-verification-v1"
BROKER_PARSER_VERSION = "resume-schema-v2"
_SUBMISSION_CLAIM_SECONDS = 30
_FRESH_CORRECTIONS = {
    "safety": "Перепиши письмо полностью и безопасно: исключи служебные инструкции, секреты и непроверенные ссылки.",
    "requirements": "Перепиши письмо полностью и выполни подтверждённые требования работодателя.",
    "special_conditions": "Перепиши письмо, сохранив подтверждённые особые условия, точные фрагменты и их позиции.",
    "formatting": "Перепиши письмо с корректным оформлением; убери пустые слоты и служебные маркеры.",
}

_ROLE_STAGE = {
    "search_planner": "discovery",
    "adaptive_search_planner": "discovery",
    "hirehi_adaptive_planner": "discovery",
    "hirehi_category": "discovery",
    "preference_compiler": "discovery",
    "resume_analyst": "evaluation",
    "required_preference_check": "evaluation",
    "writer": "letter",
    "letter_claim_check": "letter",
    "special_conditions": "letter",
    "job_summary": "reporting",
    "application_answers": "submission",
    "application_salary_estimate": "submission",
    "application_salary_rules": "submission",
    "application_salary_selection": "submission",
}


def _canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _version_markers(value: Any) -> list[str]:
    """Collect parser/search markers without copying or truncating model input."""
    result: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json")
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in {
                    "schema_version",
                    "extractor_version",
                    "source_site",
                    "search_version",
                    "algorithm_version",
                }:
                    result.append(f"{key}={nested}")
                else:
                    visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return sorted(set(result))


class BrokeredModelGateway:
    """ModelGateway-compatible facade used by spawned workflow workers.

    It deliberately has no provider object and no direct-generation method.
    Every structured operation is first made idempotent in SQL and then
    submitted/polled through :class:`ModelRequestClient`.
    """

    def __init__(
        self,
        session_id: int,
        site_id: str,
        generation: int,
        session_factory: sessionmaker[Session],
        *,
        client: ModelRequestClient | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.session_id = session_id
        self.site_id = site_id
        self.generation = max(0, int(generation))
        self.session_factory = session_factory
        self.client = client or ModelRequestClient(session_factory)
        self.poll_interval = poll_interval
        self._vacancy_id: str | None = None
        self._pipeline_key: str | None = None
        self._stage: str | None = None

    @contextmanager
    def bind(
        self,
        *,
        vacancy_id: str | int | None = None,
        pipeline_key: str | None = None,
        stage: str | None = None,
    ):
        previous = self._vacancy_id, self._pipeline_key, self._stage
        self._vacancy_id = None if vacancy_id is None else str(vacancy_id)
        self._pipeline_key = pipeline_key
        self._stage = stage
        try:
            yield self
        finally:
            self._vacancy_id, self._pipeline_key, self._stage = previous

    def set_context(
        self,
        *,
        vacancy_id: str | int | None = None,
        pipeline_key: str | None = None,
        stage: str | None = None,
    ) -> None:
        self._vacancy_id = None if vacancy_id is None else str(vacancy_id)
        self._pipeline_key = pipeline_key
        self._stage = stage

    def _versions(self, role: str, payload: Any, schema: type[BaseModel]) -> ModelVersions:
        with self.session_factory() as db:
            saved = db.get(AIModelSettings, 1)
        provider = settings.llm_provider
        model = saved.model if saved is not None else (
            "deterministic-mock" if provider == "mock" else settings.openai_model
        )
        config_revision = (
            saved.updated_at.isoformat() if saved is not None and saved.updated_at else "defaults"
        )
        markers = _version_markers(payload)
        parser_document = {
            "base": BROKER_PARSER_VERSION,
            "markers": markers,
            "search": HIREHI_SEARCH_ADAPTIVE_V3,
        }
        prompt_base = BROKER_PROMPT_VERSION
        if role in {"writer", "letter_claim_check"}:
            prompt_base = _digest({"base": BROKER_PROMPT_VERSION, "letter_claims": LETTER_CLAIMS_VERSION})
        return ModelVersions(
            model_id=f"{provider}:{model}",
            model_version=_digest({"model": model, "config_revision": config_revision})[:32],
            prompt_version=_digest({"base": prompt_base, "role": role})[:32],
            schema_version=_digest(schema.model_json_schema())[:32],
            parser_version=_digest(parser_document)[:32],
        )

    def _cancelled(self) -> bool:
        with self.session_factory() as db:
            item = db.get(JobSession, self.session_id)
            if item is None or item.status in {"STOPPING", "STOPPED", "CANCELLED", "FAILED"}:
                return True
            execution = db.scalar(
                select(SessionExecution).where(SessionExecution.session_id == self.session_id)
            )
            return bool(
                execution is not None
                and (execution.cancel_requested or execution.generation != self.generation)
            )

    def _operation(
        self,
        role: str,
        stage: str,
        input_hash: str,
        versions_hash: str,
    ) -> tuple[PipelineModelOperation, str | None]:
        vacancy_key = self._pipeline_key or self._vacancy_id or ""
        lookup = (
            PipelineModelOperation.session_id == self.session_id,
            PipelineModelOperation.site_id == self.site_id,
            PipelineModelOperation.vacancy_key == vacancy_key,
            PipelineModelOperation.stage == stage,
            PipelineModelOperation.role == role,
            PipelineModelOperation.input_hash == input_hash,
            PipelineModelOperation.versions_hash == versions_hash,
            PipelineModelOperation.generation == self.generation,
        )
        with self.session_factory() as db:
            existing = db.scalar(select(PipelineModelOperation).where(*lookup))
            if existing is not None:
                return existing, None
            pipeline_item = None
            if vacancy_key:
                pipeline_item = db.scalar(select(PipelineItem).where(
                    PipelineItem.session_id == self.session_id,
                    PipelineItem.site_id == self.site_id,
                    PipelineItem.external_id == vacancy_key,
                    PipelineItem.generation == self.generation,
                ))
            row = PipelineModelOperation(
                pipeline_item_id=pipeline_item.id if pipeline_item is not None else None,
                session_id=self.session_id,
                site_id=self.site_id,
                vacancy_key=vacancy_key,
                stage=stage,
                role=role,
                input_hash=input_hash,
                versions_hash=versions_hash,
                generation=self.generation,
                request_id=str(uuid.uuid4()),
                status="submitting",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                row = db.scalar(select(PipelineModelOperation).where(*lookup))
                if row is None:
                    raise
                return row, None
            return row, row.request_id

    def _recover_request_id(
        self,
        operation: PipelineModelOperation,
        role: str,
        stage: str,
        input_hash: str,
        versions: ModelVersions,
    ) -> tuple[str | None, str | None]:
        """Return (request id, reservation token) for one logical operation.

        Failed requests are immutable audit rows. A conditional update retargets
        the operation to one reservation token; delayed submitters are fenced
        by that token, and request creation plus relinking commits atomically.
        """
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            linked = db.get(PipelineModelOperation, operation.id)
            if linked is None:
                return None, None
            if linked.generation != self.generation or linked.status == "cancelled":
                return None, None
            if linked.request_id:
                request = db.get(ModelRequest, linked.request_id)
                if request is not None:
                    if request.status in {"queued", "running", "retry", "completed"}:
                        if request.error_code == "payload_retained_metadata":
                            raise ModelPermanentError(
                                f"Durable model operation {request.diagnostic_id} payload was retired"
                            )
                        return request.id, None
                    if request.status == "cancelled":
                        db.execute(
                            update(PipelineModelOperation)
                            .where(
                                PipelineModelOperation.id == linked.id,
                                PipelineModelOperation.generation == self.generation,
                                PipelineModelOperation.request_id == request.id,
                            )
                            .values(status="cancelled", updated_at=now)
                            .execution_options(synchronize_session=False)
                        )
                        db.commit()
                        return None, None
                    if request.status == "failed":
                        terminal_error = (
                            request.error_code == "permanent_model_error"
                            or request.error_code == "model_disabled"
                            or request.error_code == "model_retired"
                            or request.error_code == "provider_capability_unsupported"
                            or request.error_code == "provider_unauthorized"
                            or request.error_code == "model_not_configured"
                            or request.error_code == "schema_validation_failed"
                            or request.error_code == "attempt_budget_exhausted"
                            or bool(request.error_code and request.error_code.startswith("prompt_injection_"))
                        )
                        spent_attempts = db.scalar(
                            select(func.coalesce(func.sum(_durable_attempt_expression()), 0)).where(
                                ModelRequest.session_id == request.session_id,
                                ModelRequest.site_id == request.site_id,
                                ModelRequest.vacancy_id == request.vacancy_id,
                                ModelRequest.stage == request.stage,
                                ModelRequest.role == request.role,
                                ModelRequest.model_id == request.model_id,
                                ModelRequest.model_version == request.model_version,
                                ModelRequest.prompt_version == request.prompt_version,
                                ModelRequest.schema_version == request.schema_version,
                                ModelRequest.parser_version == request.parser_version,
                                ModelRequest.input_hash == request.input_hash,
                                ModelRequest.schema_ref == request.schema_ref,
                            )
                        ) or 0
                        rows = list(db.scalars(select(ModelRequest).where(
                            ModelRequest.session_id == request.session_id,
                            ModelRequest.site_id == request.site_id,
                            ModelRequest.vacancy_id == request.vacancy_id,
                            ModelRequest.stage == request.stage,
                            ModelRequest.role == request.role,
                            ModelRequest.model_id == request.model_id,
                            ModelRequest.model_version == request.model_version,
                            ModelRequest.prompt_version == request.prompt_version,
                            ModelRequest.schema_version == request.schema_version,
                            ModelRequest.parser_version == request.parser_version,
                            ModelRequest.input_hash == request.input_hash,
                            ModelRequest.schema_ref == request.schema_ref,
                        )))
                        exhausted = bool(rows) and int(spent_attempts) >= max(row.max_attempts for row in rows)
                        if exhausted and not terminal_error:
                            raise ModelPermanentError(
                                f"Durable model operation {request.diagnostic_id} exhausted its retry budget"
                            )
                        if terminal_error:
                            return request.id, None
                        claim_token = str(uuid.uuid4())
                        changed = db.execute(
                            update(PipelineModelOperation)
                            .where(
                                PipelineModelOperation.id == linked.id,
                                PipelineModelOperation.generation == self.generation,
                                PipelineModelOperation.request_id == request.id,
                                PipelineModelOperation.status != "cancelled",
                            )
                            .values(
                                request_id=claim_token,
                                diagnostic_id=None,
                                status="submitting",
                                updated_at=now,
                            )
                            .execution_options(synchronize_session=False)
                        ).rowcount
                        if changed and linked.pipeline_item_id:
                            item = db.get(PipelineItem, linked.pipeline_item_id)
                            if item is not None and item.request_id == request.id:
                                item.request_id = None
                                item.diagnostic_id = None
                                item.updated_at = now
                        db.commit()
                        return None, claim_token if changed else None

            # Covers a worker crash after broker submit but before the
            # operation-link commit. All equality terms are immutable.
            row = db.scalar(
                select(ModelRequest).where(
                    ModelRequest.session_id == self.session_id,
                    ModelRequest.site_id == self.site_id,
                    ModelRequest.vacancy_id == (self._vacancy_id or None),
                    ModelRequest.stage == stage,
                    ModelRequest.role == role,
                    ModelRequest.generation == self.generation,
                    ModelRequest.input_hash == input_hash,
                    ModelRequest.model_id == versions.model_id,
                    ModelRequest.model_version == versions.model_version,
                    ModelRequest.prompt_version == versions.prompt_version,
                    ModelRequest.schema_version == versions.schema_version,
                    ModelRequest.parser_version == versions.parser_version,
                    ModelRequest.status.in_(("queued", "running", "retry", "completed")),
                ).order_by(ModelRequest.created_at.asc())
            )
            if row is not None:
                reservation = linked.request_id
                changed = db.execute(
                    update(PipelineModelOperation)
                    .where(
                        PipelineModelOperation.id == linked.id,
                        PipelineModelOperation.generation == self.generation,
                        PipelineModelOperation.request_id == reservation,
                        PipelineModelOperation.status != "cancelled",
                    )
                    .values(
                        request_id=row.id,
                        diagnostic_id=row.diagnostic_id,
                        status=row.status,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                ).rowcount
                if changed and linked.pipeline_item_id:
                    item = db.get(PipelineItem, linked.pipeline_item_id)
                    if item is not None:
                        item.request_id = row.id
                        item.diagnostic_id = row.diagnostic_id
                        item.updated_at = now
                db.commit()
                return (row.id, None) if changed else (None, None)

            # The operation may be a legacy row with no reservation, or an
            # abandoned transaction owner. Claim it once; an active owner is
            # reclaimed only after its reservation expires. Delayed owners
            # cannot commit because their token no longer matches.
            old_token = linked.request_id
            created_claim = linked.status == "submitting" and old_token is not None
            updated_at = linked.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if created_claim and now - updated_at < timedelta(seconds=_SUBMISSION_CLAIM_SECONDS):
                return None, None
            claim_before = linked.updated_at
            claim_token = str(uuid.uuid4())
            changed = db.execute(
                update(PipelineModelOperation)
                .where(
                    PipelineModelOperation.id == linked.id,
                    PipelineModelOperation.generation == self.generation,
                    PipelineModelOperation.request_id == old_token
                    if old_token is not None
                    else PipelineModelOperation.request_id.is_(None),
                    PipelineModelOperation.status != "cancelled",
                    PipelineModelOperation.updated_at == claim_before,
                )
                .values(request_id=claim_token, diagnostic_id=None, status="submitting", updated_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            db.commit()
            return None, claim_token if changed else None

    def _submit_and_link(
        self,
        operation_id: int,
        claim_token: str,
        request: SubmitRequest,
    ) -> str | None:
        """Commit a new request and both durable links as one DB transaction."""
        if self._cancelled():
            self._finish_operation(operation_id, claim_token, "cancelled")
            raise asyncio.CancelledError
        with self.session_factory() as db:
            operation = db.get(PipelineModelOperation, operation_id)
            if (
                operation is None
                or operation.generation != self.generation
                or operation.request_id != claim_token
                or operation.status != "submitting"
            ):
                return None
            if not self._confirm_submission_claim(db, operation_id, claim_token):
                db.rollback()
                return None
            job = db.get(JobSession, self.session_id)
            execution = db.scalar(
                select(SessionExecution).where(SessionExecution.session_id == self.session_id)
            )
            if (
                job is None
                or job.status in {"STOPPING", "STOPPED", "CANCELLED", "FAILED"}
                or (execution is not None and (
                    execution.cancel_requested or execution.generation != self.generation
                ))
            ):
                operation.status = "cancelled"
                operation.updated_at = datetime.now(timezone.utc)
                db.commit()
                raise asyncio.CancelledError
            receipt = self.client.submit_in_transaction(db, request)
            now = datetime.now(timezone.utc)
            operation.request_id = receipt.request_id
            operation.diagnostic_id = receipt.diagnostic_id
            operation.status = receipt.status
            operation.updated_at = now
            if operation.pipeline_item_id:
                item = db.get(PipelineItem, operation.pipeline_item_id)
                if item is not None:
                    item.request_id = receipt.request_id
                    item.diagnostic_id = receipt.diagnostic_id
                    item.updated_at = now
            db.commit()
            return receipt.request_id

    def _confirm_submission_claim(self, db: Session, operation_id: int, claim_token: str) -> bool:
        """Take the first transactional write and serialize against claim theft."""
        changed = db.execute(
            update(PipelineModelOperation)
            .where(
                PipelineModelOperation.id == operation_id,
                PipelineModelOperation.generation == self.generation,
                PipelineModelOperation.request_id == claim_token,
                PipelineModelOperation.status == "submitting",
            )
            .values(updated_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        ).rowcount
        return bool(changed)

    def _finish_operation(self, operation_id: int, expected_request_id: str, status: str) -> None:
        with self.session_factory() as db:
            db.execute(
                update(PipelineModelOperation)
                .where(
                    PipelineModelOperation.id == operation_id,
                    PipelineModelOperation.generation == self.generation,
                    PipelineModelOperation.request_id == expected_request_id,
                )
                .values(status=status, updated_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            )
            db.commit()

    async def structured(self, role: str, payload: dict[str, Any], schema: type[T]) -> T:
        safe_payload = sanitize_untrusted_input(payload, context=f"{role}.input")
        stage = self._stage or _ROLE_STAGE.get(role, "evaluation")
        versions = self._versions(role, safe_payload, schema)
        input_hash = _digest(safe_payload)
        versions_hash = _digest(asdict(versions))
        operation, claim_token = self._operation(role, stage, input_hash, versions_hash)
        request_id: str | None = None
        while request_id is None:
            if claim_token is None:
                if self._cancelled():
                    raise asyncio.CancelledError
                request_id, claim_token = self._recover_request_id(
                    operation, role, stage, input_hash, versions
                )
                if request_id is not None:
                    break
                if claim_token is None:
                    with self.session_factory() as db:
                        current = db.get(PipelineModelOperation, operation.id)
                        if current is None:
                            if self._cancelled():
                                raise asyncio.CancelledError
                            raise ModelUnavailable("Durable model request was cancelled")
                        if current.status == "cancelled":
                            raise asyncio.CancelledError
                    await asyncio.sleep(self.poll_interval)
                    continue
            if self._cancelled():
                self._finish_operation(operation.id, claim_token, "cancelled")
                raise asyncio.CancelledError
            try:
                request_id = self._submit_and_link(operation.id, claim_token, SubmitRequest(
                    session_id=self.session_id,
                    site_id=self.site_id,
                    vacancy_id=self._vacancy_id,
                    stage=stage,
                    role=role,
                    payload=safe_payload,
                    schema=schema,
                    versions=versions,
                    generation=self.generation,
                ))
            except ModelOverloaded:
                # Free the reservation so a later invocation can retry once
                # capacity returns. The overload itself creates no request row.
                self._finish_operation(operation.id, claim_token, "failed")
                raise
            claim_token = None
            if request_id is None:
                continue

        while True:
            if self._cancelled():
                self.client.cancel(request_id)
                self._finish_operation(operation.id, request_id, "cancelled")
                raise asyncio.CancelledError
            state = self.client.poll(request_id)
            if state.status == "completed":
                if state.error_code == "payload_retained_metadata":
                    self._finish_operation(operation.id, request_id, "failed")
                    raise ModelPermanentError(
                        f"Durable model operation {state.diagnostic_id} payload was retired"
                    )
                self._finish_operation(operation.id, request_id, "completed")
                return schema.model_validate(state.result)
            if state.status == "cancelled":
                self._finish_operation(operation.id, request_id, "cancelled")
                raise asyncio.CancelledError
            if state.status in {"failed", "cancelled"}:
                self._finish_operation(operation.id, request_id, state.status)
                error_code = state.error_code or "provider_unavailable"
                if error_code.startswith("prompt_injection_"):
                    reason = error_code.removeprefix("prompt_injection_") or "detected"
                    raise PromptInjectionDetected(reason, context=stage)
                if error_code in {
                    "permanent_model_error",
                    "schema_validation_failed",
                    "attempt_budget_exhausted",
                    "model_disabled",
                    "model_retired",
                    "provider_capability_unsupported",
                    "provider_unauthorized",
                    "model_not_configured",
                }:
                    raise ModelPermanentError(
                        f"Durable model operation {state.diagnostic_id} reached a terminal failure"
                    )
                if error_code == "model_timeout":
                    raise ModelTimeout(
                        f"Durable model operation {state.diagnostic_id} timed out"
                    )
                raise ModelUnavailable(
                    f"Durable model operation {state.diagnostic_id} ended as {state.status} ({error_code})"
                )
            await asyncio.sleep(self.poll_interval)

    async def fresh_generation(
        self,
        role: str,
        payload: dict[str, Any],
        schema: type[T],
        *,
        correction_category: str,
        generation: int,
    ) -> T:
        """Start a distinct, recoverable request after local draft rejection.

        The category and ordinal are trusted application metadata. Draft text
        and validation diagnostics are deliberately never copied into prompts.
        """
        instruction = _FRESH_CORRECTIONS.get(correction_category)
        if instruction is None or generation < 1:
            raise ValueError("Unknown correction category or generation")
        fresh_payload = dict(payload)
        fresh_payload["generation_context"] = {
            "correction_category": correction_category,
            "correction_generation": generation,
        }
        requirements = str(fresh_payload.get("requirements", ""))
        fresh_payload["requirements"] = f"{requirements}\n\n{instruction}".strip()
        return await self.structured(role, fresh_payload, schema)
