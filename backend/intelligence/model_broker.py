"""Durable central scheduler for model generation requests.

Workers only submit/poll durable rows. Exactly one application-side broker is
expected to call :meth:`run_forever`; database status/lease fences still make
crash recovery and accidental overlapping broker ticks safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy import Select, case, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.intelligence.gateway import (
    ModelGateway,
    ModelOverloaded,
    ModelPermanentError,
    ModelTimeout,
    ModelUnavailable,
)
from backend.intelligence.security import (
    PromptInjectionDetected,
    assert_safe_output,
    sanitize_untrusted_input,
)
from backend.persistence.database import SessionLocal
from backend.persistence.model_request_models import (
    ModelGenerationHealth,
    ModelRequest,
    ModelResponseCache,
)
from backend.services.search_metrics import begin as begin_metrics
from backend.services.search_metrics import end as end_metrics
from backend.services.search_metrics import flush as flush_metrics
from backend.services.search_metrics import record as record_metric

GLOBAL_RUNNING_LIMIT = 3
SESSION_RUNNING_LIMIT = 2
SESSION_OUTSTANDING_LIMIT = 10
GLOBAL_OUTSTANDING_LIMIT = 30
LOGICAL_DEADLINE_SECONDS = 180.0
MIN_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 300.0
QUEUE_WAIT_SECONDS = GLOBAL_OUTSTANDING_LIMIT * LOGICAL_DEADLINE_SECONDS + 4 * MAX_RETRY_SECONDS

_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|password|passwd|proxy[_-]?password|secret|client[_-]?secret|"
    r"private[_-]?key|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"authorization|cookie|session[_-]?cookie|credentials?)$",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+)[A-Za-z0-9._~+/=-]{12,}|"
    r"\b(?:sk|sess|key|ghp|xoxb)-[A-Za-z0-9_-]{12,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|proxy[_-]?password|secret|client[_-]?secret|"
    r"private[_-]?key|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"authorization|cookie|session[_-]?cookie|credentials?)\s*([:=])\s*([^\s,;]+)"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _storage_safe(value: Any) -> Any:
    """Return a full JSON value while redacting credential-shaped content."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.fullmatch(str(key)) else _storage_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_storage_safe(item) for item in value]
    if isinstance(value, str):
        value = _SECRET_VALUE.sub("[REDACTED]", value)
        return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    return value


def canonical_json(value: Any) -> str:
    sanitized = sanitize_untrusted_input(value, context="model_broker.input")
    return json.dumps(
        _storage_safe(sanitized),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def schema_reference(schema: type[BaseModel]) -> str:
    reference = f"{schema.__module__}:{schema.__qualname__}"
    if "<locals>" in reference:
        raise ValueError("Broker schemas must be importable top-level Pydantic models")
    return reference


def resolve_schema(reference: str) -> type[BaseModel]:
    module_name, separator, qualname = reference.partition(":")
    if not separator or not module_name.startswith("backend."):
        raise ValueError("Model schema reference is outside the application allowlist")
    value: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        value = getattr(value, part)
    if not isinstance(value, type) or not issubclass(value, BaseModel):
        raise TypeError("Model schema reference is not a Pydantic model")
    return value


@dataclass(frozen=True)
class ModelVersions:
    model_id: str
    model_version: str
    prompt_version: str
    schema_version: str
    parser_version: str


@dataclass(frozen=True)
class SubmitRequest:
    session_id: int
    site_id: str
    stage: str
    role: str
    payload: Any
    schema: type[BaseModel]
    versions: ModelVersions
    vacancy_id: str | int | None = None
    generation: int = 0
    max_attempts: int = 4


@dataclass(frozen=True)
class RequestReceipt:
    request_id: str
    diagnostic_id: str
    status: str
    cache_hit: bool
    deadline_at: datetime


@dataclass(frozen=True)
class RequestState:
    request_id: str
    diagnostic_id: str
    session_id: int
    site_id: str
    vacancy_id: str | None
    stage: str
    status: str
    attempt: int
    generation: int
    deadline_at: datetime
    result: dict[str, Any] | None
    error_code: str | None


@dataclass(frozen=True)
class ProviderCall:
    request_id: str
    diagnostic_id: str
    session_id: int
    site_id: str
    vacancy_id: str | None
    stage: str
    generation: int
    attempt: int
    role: str
    payload: dict[str, Any]
    schema_ref: str
    remaining_seconds: float


class ModelProvider(Protocol):
    async def generate(self, call: ProviderCall) -> BaseModel | dict[str, Any]: ...

    async def catalog_status(self) -> dict[str, Any]: ...


class GatewayModelProvider:
    """Non-recursive production provider used only by the central broker."""

    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self.gateway = gateway

    async def generate(self, call: ProviderCall) -> BaseModel:
        # A fresh gateway per running request avoids reintroducing a shared
        # per-worker lock as a hidden global semaphore. Tests may inject one.
        gateway = self.gateway or ModelGateway()
        return await gateway.direct_structured(
            call.role,
            call.payload,
            resolve_schema(call.schema_ref),
            logical_timeout=call.remaining_seconds,
            diagnostic_id=call.diagnostic_id,
        )

    async def catalog_status(self) -> dict[str, Any]:
        return await (self.gateway or ModelGateway()).status()


class ModelRequestError(RuntimeError):
    pass


class ModelRequestFailed(ModelRequestError):
    def __init__(self, state: RequestState) -> None:
        super().__init__(f"Model request {state.diagnostic_id} ended as {state.status}")
        self.state = state


def _attempt_history_filters(request: SubmitRequest, *, input_hash: str, schema_ref: str):
    """Immutable fields identifying all rows for one recoverable operation."""
    return (
        ModelRequest.session_id == request.session_id,
        ModelRequest.site_id == request.site_id,
        ModelRequest.vacancy_id == (None if request.vacancy_id is None else str(request.vacancy_id)),
        ModelRequest.stage == request.stage,
        ModelRequest.role == request.role,
        # Generation is a runtime lease/version fence, not a new logical
        # provider budget. Runtime takeover must keep counting prior rows.
        ModelRequest.model_id == request.versions.model_id,
        ModelRequest.model_version == request.versions.model_version,
        ModelRequest.prompt_version == request.versions.prompt_version,
        ModelRequest.schema_version == request.versions.schema_version,
        ModelRequest.parser_version == request.versions.parser_version,
        ModelRequest.input_hash == input_hash,
        ModelRequest.schema_ref == schema_ref,
    )


def _durable_attempt_expression():
    """Count a row as consumed even when it expired before its first claim."""
    return case((ModelRequest.attempt < 1, 1), else_=ModelRequest.attempt)


class ModelRequestClient:
    """DB-only worker API; it never invokes a provider in the worker process."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        clock: Callable[[], datetime] = utcnow,
        logical_deadline_seconds: float = LOGICAL_DEADLINE_SECONDS,
    ) -> None:
        if logical_deadline_seconds <= 0 or logical_deadline_seconds > LOGICAL_DEADLINE_SECONDS:
            raise ValueError("Logical model deadline must be in (0, 180] seconds")
        self._session_factory = session_factory
        self._clock = clock
        self._deadline_seconds = logical_deadline_seconds

    def submit(self, request: SubmitRequest) -> RequestReceipt:
        with self._session_factory() as db:
            receipt = self.submit_in_transaction(db, request)
            db.commit()
            return receipt

    def submit_in_transaction(self, db: Session, request: SubmitRequest) -> RequestReceipt:
        """Insert one bounded request into a caller-owned transaction.

        Workflow operations use this to commit the request row and its durable
        operation link atomically. Ordinary broker clients should use submit().
        """
        if request.generation < 0 or request.max_attempts < 1:
            raise ValueError("Invalid request generation or attempt limit")
        if not request.site_id.strip() or not request.stage.strip() or not request.role.strip():
            raise ValueError("site_id, stage and role are required")
        if db.get_bind().dialect.name == "sqlite":
            # SQLite has no row-level SELECT FOR UPDATE. A no-op write to an
            # impossible UUID acquires its single writer slot before reading
            # durable admission counts.
            db.execute(
                update(ModelRequest)
                .where(ModelRequest.id == "__queue_admission_lock__")
                .values(available_at=ModelRequest.available_at)
                .execution_options(synchronize_session=False)
            )
        canonical_input = canonical_json(request.payload)
        if not isinstance(json.loads(canonical_input), dict):
            raise ValueError("Model request payload must be a JSON object")
        input_hash = hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()
        schema_ref = schema_reference(request.schema)
        version_document = canonical_json(
            {
                "role": request.role,
                "schema_ref": schema_ref,
                "model_id": request.versions.model_id,
                "model_version": request.versions.model_version,
                "prompt_version": request.versions.prompt_version,
                "schema_version": request.versions.schema_version,
                "parser_version": request.versions.parser_version,
            }
        )
        cache_key = hashlib.sha256(
            canonical_input.encode("utf-8") + b"\0" + version_document.encode("utf-8")
        ).hexdigest()
        now = self._clock()
        # Before its first provider claim this is the admission/queue deadline.
        # The first claim replaces it with the execution deadline, which then
        # remains fixed across retries and broker restarts.
        deadline = now + timedelta(seconds=QUEUE_WAIT_SECONDS)
        request_id = str(uuid.uuid4())
        diagnostic_id = str(uuid.uuid4())
        spent_attempts = db.scalar(
            select(func.coalesce(func.sum(_durable_attempt_expression()), 0)).where(
                *_attempt_history_filters(request, input_hash=input_hash, schema_ref=schema_ref)
            )
        ) or 0
        remaining_attempts = max(0, request.max_attempts - int(spent_attempts))
        cached = db.scalar(
            select(ModelResponseCache).where(
                ModelResponseCache.session_id == request.session_id,
                ModelResponseCache.cache_key == cache_key,
            )
        )
        budget_exhausted = cached is None and remaining_attempts == 0
        if cached is None and not budget_exhausted:
            outstanding = ModelRequest.status.in_(("queued", "running", "retry"))
            session_count = db.scalar(
                select(func.count(ModelRequest.id)).where(
                    ModelRequest.session_id == request.session_id,
                    outstanding,
                )
            ) or 0
            global_count = db.scalar(
                select(func.count(ModelRequest.id)).where(outstanding)
            ) or 0
            if session_count >= SESSION_OUTSTANDING_LIMIT or global_count >= GLOBAL_OUTSTANDING_LIMIT:
                raise ModelOverloaded()
        status = "completed" if cached is not None else "failed" if budget_exhausted else "queued"
        row = ModelRequest(
            id=request_id,
            diagnostic_id=diagnostic_id,
            session_id=request.session_id,
            site_id=request.site_id,
            vacancy_id=None if request.vacancy_id is None else str(request.vacancy_id),
            stage=request.stage,
            role=request.role,
            schema_ref=schema_ref,
            status=status,
            generation=request.generation,
            attempt=0,
            max_attempts=max(1, remaining_attempts),
            model_id=request.versions.model_id,
            model_version=request.versions.model_version,
            prompt_version=request.versions.prompt_version,
            schema_version=request.versions.schema_version,
            parser_version=request.versions.parser_version,
            input_hash=input_hash,
            cache_key=cache_key,
            canonical_input=canonical_input,
            canonical_output=cached.canonical_output if cached else None,
            cache_source_request_id=cached.source_request_id if cached else None,
            created_at=now,
            available_at=now,
            deadline_at=deadline,
            completed_at=now if cached else None,
            error_code="attempt_budget_exhausted" if budget_exhausted else None,
        )
        if budget_exhausted:
            row.completed_at = now
        db.add(row)
        return RequestReceipt(
            request_id=request_id,
            diagnostic_id=diagnostic_id,
            status=status,
            cache_hit=cached is not None,
            deadline_at=deadline,
        )

    def poll(self, request_id: str) -> RequestState:
        with self._session_factory() as db:
            row = db.get(ModelRequest, request_id)
            if row is None:
                raise KeyError(request_id)
            output = json.loads(row.canonical_output) if row.canonical_output is not None else None
            return RequestState(
                request_id=row.id,
                diagnostic_id=row.diagnostic_id,
                session_id=row.session_id,
                site_id=row.site_id,
                vacancy_id=row.vacancy_id,
                stage=row.stage,
                status=row.status,
                attempt=row.attempt,
                generation=row.generation,
                deadline_at=_aware(row.deadline_at),
                result=output,
                error_code=row.error_code,
            )

    async def wait(self, request_id: str, *, poll_interval: float = 0.1) -> RequestState:
        while True:
            state = self.poll(request_id)
            if state.status == "completed":
                return state
            if state.status == "cancelled":
                raise asyncio.CancelledError
            if state.status == "failed":
                raise ModelRequestFailed(state)
            remaining = (state.deadline_at - _aware(self._clock())).total_seconds()
            if remaining <= 0:
                now = self._clock()
                with self._session_factory() as db:
                    db.execute(
                        update(ModelRequest)
                        .where(
                            ModelRequest.id == request_id,
                            ModelRequest.status.in_(("queued", "running", "retry")),
                        )
                        .values(
                            status="failed",
                            completed_at=now,
                            lease_owner=None,
                            heartbeat_at=None,
                            error_code=(
                                "queue_wait_exceeded"
                                if state.status == "queued"
                                else "deadline_exceeded"
                            ),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    db.commit()
                terminal = self.poll(request_id)
                if terminal.status == "completed":
                    return terminal
                raise ModelRequestFailed(terminal)
            await asyncio.sleep(min(poll_interval, remaining))

    def cancel(self, request_id: str) -> bool:
        now = self._clock()
        with self._session_factory() as db:
            changed = db.execute(
                update(ModelRequest)
                .where(
                    ModelRequest.id == request_id,
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
            ).rowcount
            db.commit()
        return bool(changed)


class ModelRequestBroker(ModelRequestClient):
    """Central fair scheduler with durable claims and bounded concurrency."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        provider: ModelProvider | None = None,
        clock: Callable[[], datetime] = utcnow,
        logical_deadline_seconds: float = LOGICAL_DEADLINE_SECONDS,
        retry_base_seconds: float = MIN_RETRY_SECONDS,
    ) -> None:
        super().__init__(
            session_factory,
            clock=clock,
            logical_deadline_seconds=logical_deadline_seconds,
        )
        if retry_base_seconds < MIN_RETRY_SECONDS or retry_base_seconds > MAX_RETRY_SECONDS:
            raise ValueError("Retry base must be between 5 and 300 seconds")
        self.provider = provider or GatewayModelProvider()
        self._retry_base = retry_base_seconds
        self._owner = uuid.uuid4().hex
        self._last_site: str | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    def _eligible(self, now: datetime) -> Select[tuple[ModelRequest]]:
        return (
            select(ModelRequest)
            .where(
                ModelRequest.status.in_(("queued", "retry")),
                ModelRequest.available_at <= now,
                ModelRequest.deadline_at > now,
            )
            .order_by(ModelRequest.created_at.asc(), ModelRequest.id.asc())
        )

    def _expire_waiting(self, db: Session, now: datetime) -> int:
        # Requests persisted by older versions used the 180 second execution
        # window for queueing too. Give those unclaimed rows the current finite
        # queue allowance anchored at creation. Never extend a request more
        # than once or let broker ticks turn this into an unbounded deadline.
        expired_unclaimed = list(db.scalars(
            select(ModelRequest).where(
                ModelRequest.status == "queued",
                ModelRequest.attempt == 0,
                ModelRequest.deadline_at <= now,
            )
        ))
        legacy = 0
        legacy_window = timedelta(seconds=self._deadline_seconds)
        queue_window = timedelta(seconds=QUEUE_WAIT_SECONDS)
        for row in expired_unclaimed:
            created_at = _aware(row.created_at)
            deadline_at = _aware(row.deadline_at)
            if deadline_at > created_at + legacy_window:
                continue
            row.deadline_at = created_at + queue_window
            legacy += 1
        changed = db.execute(
            update(ModelRequest)
            .where(
                ModelRequest.status.in_(("queued", "retry")),
                ModelRequest.deadline_at <= now,
            )
            .values(
                status="failed",
                completed_at=now,
                error_code=case(
                    (ModelRequest.status == "queued", "queue_wait_exceeded"),
                    else_="deadline_exceeded",
                ),
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        return int(changed or 0) + int(legacy or 0)

    def _next_candidate(self, db: Session, now: datetime) -> ModelRequest | None:
        running = db.execute(
            select(ModelRequest.session_id, func.count(ModelRequest.id))
            .where(ModelRequest.status == "running")
            .group_by(ModelRequest.session_id)
        ).all()
        per_session = Counter({session_id: count for session_id, count in running})
        rows = list(db.scalars(self._eligible(now)))
        by_site: dict[str, list[ModelRequest]] = {}
        for row in rows:
            by_site.setdefault(row.site_id, []).append(row)
        sites = sorted(by_site)
        if not sites:
            return None
        if self._last_site is not None:
            # The previous site can disappear after its only row is claimed.
            # Continue after its lexical position rather than resetting to the
            # first busy site, which would starve later sites under HH load.
            pivot = next(
                (index for index, site in enumerate(sites) if site > self._last_site),
                0,
            )
            sites = sites[pivot:] + sites[:pivot]
        for site in sites:
            # Capacity-blocked rows are temporarily ineligible. Preserve FIFO
            # among rows that can run now so one saturated session cannot
            # starve every later session sharing the same adapter site.
            head = next(
                (
                    row
                    for row in by_site[site]
                    if per_session[row.session_id] < SESSION_RUNNING_LIMIT
                ),
                None,
            )
            if head is not None:
                self._last_site = site
                return head
        return None

    def _claim_one(self) -> str | None:
        now = self._clock()
        with self._session_factory() as db:
            self._expire_waiting(db, now)
            global_running = db.scalar(
                select(func.count(ModelRequest.id)).where(ModelRequest.status == "running")
            ) or 0
            if global_running >= GLOBAL_RUNNING_LIMIT:
                db.commit()
                return None
            candidate = self._next_candidate(db, now)
            if candidate is None:
                db.commit()
                return None
            changed = db.execute(
                update(ModelRequest)
                .where(
                    ModelRequest.id == candidate.id,
                    ModelRequest.status.in_(("queued", "retry")),
                    ModelRequest.available_at <= now,
                    ModelRequest.deadline_at > now,
                )
                .values(
                    status="running",
                    attempt=ModelRequest.attempt + 1,
                    started_at=now,
                    heartbeat_at=now,
                    lease_owner=self._owner,
                    deadline_at=(
                        now + timedelta(seconds=self._deadline_seconds)
                        if candidate.attempt == 0
                        else candidate.deadline_at
                    ),
                    error_code=None,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            db.commit()
            return candidate.id if changed else None

    async def run_once(self) -> int:
        launched = 0
        while self.active_count < GLOBAL_RUNNING_LIMIT:
            request_id = self._claim_one()
            if request_id is None:
                break
            task = asyncio.create_task(self._execute(request_id))
            self._tasks[request_id] = task
            task.add_done_callback(lambda _, key=request_id: self._tasks.pop(key, None))
            launched += 1
        return launched

    async def run_forever(
        self,
        *,
        tick_seconds: float = 0.05,
        stale_after_seconds: float = 30.0,
    ) -> None:
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must be non-negative")
        self._stop.clear()
        while not self._stop.is_set():
            # Recovery is periodic, not only a startup action: a lease that is
            # still fresh when a replacement broker starts must become
            # runnable after its heartbeat actually goes stale.
            self.recover_stale(stale_after_seconds=stale_after_seconds)
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=tick_seconds)

    async def stop(self, *, drain: bool = True) -> None:
        self._stop.set()
        if drain and self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def drain(self, *, timeout: float = 5.0) -> None:
        async def work() -> None:
            while True:
                await self.run_once()
                if self._tasks:
                    await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
                    continue
                with self._session_factory() as db:
                    actionable = db.scalar(
                        select(func.count(ModelRequest.id)).where(
                            ModelRequest.status.in_(("queued", "retry")),
                            ModelRequest.available_at <= self._clock(),
                            ModelRequest.deadline_at > self._clock(),
                        )
                    )
                if not actionable:
                    return

        await asyncio.wait_for(work(), timeout=timeout)

    async def _heartbeat(self, request_id: str, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                with self._session_factory() as db:
                    db.execute(
                        update(ModelRequest)
                        .where(
                            ModelRequest.id == request_id,
                            ModelRequest.status == "running",
                            ModelRequest.lease_owner == self._owner,
                        )
                        .values(heartbeat_at=self._clock())
                        .execution_options(synchronize_session=False)
                    )
                    db.commit()
        except asyncio.CancelledError:
            return

    async def _execute(self, request_id: str) -> None:
        with self._session_factory() as db:
            row = db.get(ModelRequest, request_id)
            if row is None or row.status != "running" or row.lease_owner != self._owner:
                return
            deadline = _aware(row.deadline_at)
            payload = json.loads(row.canonical_input)
            generation_context = payload.get("generation_context") if isinstance(payload, dict) else None
            repair_category = (
                generation_context.get("correction_category")
                if isinstance(generation_context, dict)
                else None
            )
            repair_generation = (
                generation_context.get("correction_generation")
                if isinstance(generation_context, dict)
                else None
            )
            call = ProviderCall(
                request_id=row.id,
                diagnostic_id=row.diagnostic_id,
                session_id=row.session_id,
                site_id=row.site_id,
                vacancy_id=row.vacancy_id,
                stage=row.stage,
                generation=row.generation,
                attempt=row.attempt,
                role=row.role,
                payload=payload,
                schema_ref=row.schema_ref,
                remaining_seconds=max(0.0, (deadline - _aware(self._clock())).total_seconds()),
            )
            started_at = _aware(row.started_at) if row.started_at else _aware(self._clock())
            queued_at = _aware(row.created_at)
        metrics_token = begin_metrics()
        provider_started = perf_counter()
        record_metric("model_queue", {
            "stage": call.stage,
            "role": call.role,
            "vacancy_id": call.vacancy_id,
            "diagnostic_id": call.diagnostic_id,
            "queue_seconds": max(0.0, (started_at - queued_at).total_seconds()),
            "attempt": call.attempt,
            "retry_count": max(0, call.attempt - 1),
        })
        if call.attempt > 1:
            record_metric("model_recovery", {
                "stage": call.stage,
                "role": call.role,
                "vacancy_id": call.vacancy_id,
                "diagnostic_id": call.diagnostic_id,
                "attempt": call.attempt,
                "retry_count": call.attempt - 1,
            })
        if repair_category in {"safety", "requirements", "special_conditions", "formatting"}:
            record_metric("model_repair", {
                "stage": call.stage,
                "role": call.role,
                "vacancy_id": call.vacancy_id,
                "diagnostic_id": call.diagnostic_id,
                "correction_category": repair_category,
                "correction_generation": repair_generation,
            })
        heartbeat = asyncio.create_task(
            self._heartbeat(request_id, max(1.0, min(10.0, call.remaining_seconds / 3)))
        )
        try:
            if call.remaining_seconds <= 0:
                raise TimeoutError
            async with asyncio.timeout(call.remaining_seconds):
                result = await self.provider.generate(call)
            if _aware(self._clock()) >= deadline:
                raise TimeoutError
            output_value = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
            if not isinstance(output_value, dict):
                raise TypeError("Provider result must be a JSON object")
            schema = resolve_schema(call.schema_ref)
            output_value = schema.model_validate(output_value).model_dump(mode="json")
            # Defense in depth for injected/test providers: the production
            # gateway already validates its schema, but no provider result may
            # bypass schema validation or persist credential-shaped material.
            output_value = schema.model_validate(_storage_safe(output_value)).model_dump(
                mode="json"
            )
            assert_safe_output(output_value, context="model_broker.output")
            canonical_output = json.dumps(
                output_value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self._complete(request_id, canonical_output)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail_or_retry(request_id, "model_timeout")
        except PromptInjectionDetected as exc:
            reason = re.sub(r"[^a-z0-9_]+", "_", exc.reason_code.lower())[:70]
            self._fail_or_retry(
                request_id,
                f"prompt_injection_{reason or 'detected'}",
                force_terminal=True,
            )
        except ModelPermanentError as exc:
            code = re.sub(r"[^a-z0-9_]+", "_", exc.error_code.lower())[:100]
            self._fail_or_retry(request_id, code or "permanent_model_error", force_terminal=True)
        except ModelTimeout:
            self._fail_or_retry(request_id, "model_timeout")
        except ModelUnavailable as exc:
            code = re.sub(r"[^a-z0-9_]+", "_", exc.error_code.lower())[:100]
            self._fail_or_retry(request_id, code or "provider_unavailable")
        except (TypeError, ValueError):
            self._fail_or_retry(request_id, "schema_validation_failed", force_terminal=True)
        except Exception as exc:
            error_code = re.sub(r"[^a-z0-9_]+", "_", exc.__class__.__name__.lower())[:100]
            self._fail_or_retry(request_id, error_code or "provider_error")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            record_metric("model_provider", {
                "stage": call.stage,
                "role": call.role,
                "vacancy_id": call.vacancy_id,
                "diagnostic_id": call.diagnostic_id,
                "provider_seconds": max(0.0, perf_counter() - provider_started),
                "attempt": call.attempt,
                "retry_count": max(0, call.attempt - 1),
            })
            try:
                with self._session_factory() as db:
                    flush_metrics(db, call.session_id)
                    db.commit()
            finally:
                end_metrics(metrics_token)

    def _complete(self, request_id: str, canonical_output: str) -> bool:
        now = self._clock()
        with self._session_factory() as db:
            row = db.get(ModelRequest, request_id)
            if row is None or row.status != "running" or row.lease_owner != self._owner:
                return False
            row.status = "completed"
            row.canonical_output = canonical_output
            row.completed_at = now
            row.heartbeat_at = None
            row.lease_owner = None
            existing = db.scalar(
                select(ModelResponseCache).where(
                    ModelResponseCache.session_id == row.session_id,
                    ModelResponseCache.cache_key == row.cache_key,
                )
            )
            if existing is None:
                db.add(
                    ModelResponseCache(
                        session_id=row.session_id,
                        cache_key=row.cache_key,
                        source_request_id=row.id,
                        canonical_output=canonical_output,
                        created_at=now,
                    )
                )
            health = db.get(ModelGenerationHealth, 1)
            if health is None:
                health = ModelGenerationHealth(id=1, success_count=0, failure_count=0)
                db.add(health)
            health.success_count += 1
            health.last_success_request_id = row.id
            health.last_success_at = now
            db.commit()
            return True

    def _fail_or_retry(self, request_id: str, error_code: str, *, force_terminal: bool = False) -> None:
        now = self._clock()
        with self._session_factory() as db:
            row = db.get(ModelRequest, request_id)
            if row is None or row.status != "running" or row.lease_owner != self._owner:
                return
            backoff = min(MAX_RETRY_SECONDS, self._retry_base * (2 ** max(0, row.attempt - 1)))
            retry_at = now + timedelta(seconds=backoff)
            deadline = _aware(row.deadline_at)
            can_retry = (
                not force_terminal
                and row.attempt < row.max_attempts
                and _aware(retry_at) < deadline
            )
            row.status = "retry" if can_retry else "failed"
            row.available_at = retry_at if can_retry else row.available_at
            row.completed_at = None if can_retry else now
            row.started_at = None
            row.heartbeat_at = None
            row.lease_owner = None
            row.error_code = error_code
            health = db.get(ModelGenerationHealth, 1)
            if health is None:
                health = ModelGenerationHealth(id=1, success_count=0, failure_count=0)
                db.add(health)
            health.failure_count += 1
            health.last_failure_request_id = row.id
            health.last_failure_at = now
            health.last_error_code = error_code
            db.commit()

    def recover_stale(self, *, stale_after_seconds: float = 30.0) -> int:
        """Idempotently release stale running leases without replaying completed work."""
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds must be non-negative")
        now = self._clock()
        cutoff = now - timedelta(seconds=stale_after_seconds)
        changed = 0
        with self._session_factory() as db:
            rows = list(db.scalars(select(ModelRequest).where(ModelRequest.status == "running")))
            for row in rows:
                last_seen = row.heartbeat_at or row.started_at
                if last_seen is not None and _aware(last_seen) > _aware(cutoff):
                    continue
                if _aware(row.deadline_at) <= _aware(now) or row.attempt >= row.max_attempts:
                    row.status = "failed"
                    row.completed_at = now
                    row.error_code = "stale_deadline" if _aware(row.deadline_at) <= _aware(now) else "stale_attempt_limit"
                else:
                    backoff = min(
                        MAX_RETRY_SECONDS,
                        self._retry_base * (2 ** max(0, row.attempt - 1)),
                    )
                    retry_at = now + timedelta(seconds=backoff)
                    if _aware(retry_at) >= _aware(row.deadline_at):
                        row.status = "failed"
                        row.completed_at = now
                        row.error_code = "stale_deadline"
                    else:
                        row.status = "retry"
                        row.available_at = retry_at
                        row.error_code = "stale_recovered"
                row.started_at = None
                row.heartbeat_at = None
                row.lease_owner = None
                changed += 1
            db.commit()
        return changed

    async def catalog_availability(self) -> dict[str, Any]:
        """Provider catalog reachability; this is not generation health."""
        return await self.provider.catalog_status()

    def generation_health(self) -> dict[str, Any]:
        """Observed real generation outcomes; no catalog inference is made."""
        with self._session_factory() as db:
            health = db.get(ModelGenerationHealth, 1)
            running = db.scalar(
                select(func.count(ModelRequest.id)).where(ModelRequest.status == "running")
            ) or 0
            queued = db.scalar(
                select(func.count(ModelRequest.id)).where(
                    or_(ModelRequest.status == "queued", ModelRequest.status == "retry")
                )
            ) or 0
            if health is None:
                return {
                    "healthy": None,
                    "success_count": 0,
                    "failure_count": 0,
                    "running": running,
                    "queued": queued,
                }
            healthy = (
                health.last_success_at is not None
                and (
                    health.last_failure_at is None
                    or _aware(health.last_success_at) >= _aware(health.last_failure_at)
                )
            )
            return {
                "healthy": healthy,
                "success_count": health.success_count,
                "failure_count": health.failure_count,
                "last_success_request_id": health.last_success_request_id,
                "last_success_at": health.last_success_at,
                "last_failure_request_id": health.last_failure_request_id,
                "last_failure_at": health.last_failure_at,
                "last_error_code": health.last_error_code,
                "running": running,
                "queued": queued,
            }
