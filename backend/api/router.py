from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlparse, urlunparse

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.adapters import adapter_registry
from backend.browser.executor import BrowserExecutor
from backend.browser.sessions import (
    acquire_browser_lease,
    close_browser,
    get_browser,
    release_browser_lease,
    set_browser,
)
from backend.config import settings
from backend.intelligence.gateway import ModelGateway, ModelUnavailable
from backend.intelligence.model_broker import ModelRequestBroker
from backend.intelligence.model_config import (
    auth_headers,
    is_local_url,
    model_http_client,
    normalize_base_url,
    validate_base_url,
)
from backend.intelligence.security import sanitize_untrusted_input
from backend.persistence.crypto import decrypt_secret, encrypt_secret
from backend.persistence.database import get_db
from backend.persistence.execution_models import (
    SessionExecution,
    SessionIdempotencyKey,
    SiteExecutionLease,
)
from backend.persistence.model_request_models import ModelRequest
from backend.persistence.models import (
    AIModelSettings,
    BrowserEvent,
    JobSession,
    Notification,
    SavedResumeSource,
    SessionResumeSnapshot,
)

# The API process owns only durable state and worker IPC. Workflow/Playwright
# are imported lazily by the spawned runtime worker.
from backend.runtime import runtime_supervisor
from backend.runtime.lifecycle import (
    canonical_payload_hash,
    claim_site_lease,
    ensure_execution,
    find_idempotency,
    idempotency_record,
    release_site_lease,
    request_cancel,
    request_start,
)
from backend.schemas.domain import (
    SessionStatus,
    SiteResumeSnapshot,
)
from backend.services.private_text import _unseal_private
from backend.services.resume_session import (
    ResumeImportError,
    ResumeImportUnavailable,
    SavedResumeSourceNotFound,
    _private_gender_value,
    _redacted_snapshot,
    confirm_saved_resume_source,
    delete_saved_resume_source,
    extract_resume,
    issue_preview_token,
    list_saved_resume_sources,
    load_saved_resume_data,
    persist_session_snapshot,
    public_preview,
    refresh_saved_resume_source,
    saved_resume_source_record,
    update_saved_resume_gender,
    uses_saved_resume_data,
    validate_adapter_resume_url,
)

router = APIRouter(prefix="/api")
_SAVED_RESUME_REFRESH_ERROR = 'Обновите данные резюме во вкладке «Профиль»'
_SAVED_RESUME_LAUNCH_ERRORS = {
    "hh": (
        'Для сайта HH.ru не загружено резюме, проверьте раздел "Профиль"',
        'Для сайта HH.ru не удалось извлечь необходимые данные из резюме, проверьте раздел "Профиль"',
    ),
    "hirehi": (
        'Для сайта HireHi не загружено резюме, проверьте раздел "Профиль"',
        'Для сайта HireHi не удалось извлечь необходимые данные из резюме, проверьте раздел "Профиль"',
    ),
    "zarplata": (
        'Для сайта Zarplata.ru не загружено резюме, проверьте раздел "Профиль"',
        'Для сайта Zarplata.ru не удалось извлечь необходимые данные из резюме, проверьте раздел "Профиль"',
    ),
}


def _saved_resume_launch_error(adapter_id: str, *, missing: bool) -> str:
    errors = _SAVED_RESUME_LAUNCH_ERRORS.get(adapter_id)
    if errors is None:
        return _SAVED_RESUME_REFRESH_ERROR
    return errors[0 if missing else 1]


def _validate_session_resume_snapshot(snapshot: SessionResumeSnapshot, adapter_id: str) -> SiteResumeSnapshot:
    """Validate the durable full copy before starting or resuming a session."""
    try:
        full = SiteResumeSnapshot.model_validate(snapshot.full_snapshot)
        public, _ = _redacted_snapshot(full)
        if (
            snapshot.source_site != adapter_id
            or full.source_site != adapter_id
            or snapshot.source_url_hash != full.source_url_hash
            or snapshot.content_hash != public.content_hash
            or SiteResumeSnapshot.model_validate(snapshot.snapshot).content_hash != snapshot.content_hash
        ):
            raise ValueError("snapshot integrity mismatch")
        _private_gender_value(snapshot.private_view)
    except Exception as exc:
        raise HTTPException(422, "Снимок резюме сессии повреждён; запустите новую сессию") from exc
    return full


class _LazyWorkflowManager:
    """Compatibility shim for report/legacy tests.

    Accessing this object imports workflow only for an explicit legacy API
    operation; normal session creation and start stay process-isolated.
    """

    def __getattr__(self, name):
        from backend.orchestrator.workflow import workflow_manager as manager
        return getattr(manager, name)


workflow_manager = _LazyWorkflowManager()
_session_create_lock = Lock()
_profile_operation_guard = Lock()
_profile_operation_locks: dict[str, Lock] = {}


def _profile_operation_lock(site_id: str) -> Lock:
    """Return one process-wide gate for preview/refresh browser profiles."""
    with _profile_operation_guard:
        return _profile_operation_locks.setdefault(site_id, Lock())


def _assert_durable_profile_free(db: Session, site_id: str) -> None:
    """Reject preview/refresh while a durable session owns the profile."""
    if not hasattr(db, "get"):
        return
    lease = db.get(SiteExecutionLease, site_id)
    if lease is None:
        return
    owner = db.get(JobSession, lease.session_id)
    if owner is None or owner.status in {
        SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.FAILED, "CANCELLED",
    }:
        if hasattr(db, "delete"):
            db.delete(lease)
            if hasattr(db, "commit"):
                db.commit()
        return
    raise HTTPException(409, "Профиль сайта занят другой сессией")


def _check_model_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Недопустимый источник запроса")
    origin = request.headers.get("origin")
    allowed = {"http://127.0.0.1:5173", "http://localhost:5173"}
    if (
        origin
        and origin not in allowed
        and origin != f"{request.url.scheme}://{request.url.netloc}"
    ):
        raise HTTPException(403, "Недопустимый источник запроса")


class ModelSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=8192)

    @field_validator("model", "api_key", mode="before")
    @classmethod
    def strip_values(cls, value):
        return value.strip() if isinstance(value, str) else value


class ModelModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2048)
    api_key: str | None = Field(default=None, max_length=8192)

    @field_validator("api_key", mode="before")
    @classmethod
    def strip_key(cls, value):
        return value.strip() if isinstance(value, str) else value


def _no_store(data):
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.get("/model/settings")
def get_model_settings(db: Session = Depends(get_db)):
    item = db.get(AIModelSettings, 1)
    return _no_store(
        {
            "base_url": item.base_url if item else settings.openai_base_url,
            "model": item.model if item else settings.openai_model,
            "has_api_key": bool(item and item.encrypted_api_key),
            "masked_key": "••••••••" if item and item.encrypted_api_key else "",
        }
    )


async def _models(base_url: str, key: str) -> list[str]:
    async with model_http_client(base_url, 15) as client:
        response = await client.get(
            normalize_base_url(base_url) + "/models", headers=auth_headers(key)
        )
        response.raise_for_status()
        if (
            response.headers.get("content-length", "0").isdigit()
            and int(response.headers["content-length"]) > 2 * 1024 * 1024
        ):
            raise ValueError("Ответ слишком большой")
        body = await response.aread()
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("Ответ слишком большой")
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Некорректный список моделей")
    models = {
        x["id"]
        for x in data["data"]
        if isinstance(x, dict) and isinstance(x.get("id"), str) and 0 < len(x["id"]) <= 255
    }
    if not models:
        raise ValueError("Список моделей пуст")
    return sorted(models)


def _model_connection(
    payload: ModelModelsIn | ModelSettingsIn, item: AIModelSettings | None,
) -> tuple[str, str]:
    try:
        base = validate_base_url(
            payload.base_url,
            (settings.openai_base_url, item.base_url if item else ""),
        )
    except ValueError as exc:
        # Validation messages are authored locally and contain no key/server body.
        raise HTTPException(400, str(exc)) from exc
    key = payload.api_key
    if not key and item and item.encrypted_api_key and normalize_base_url(item.base_url) == base:
        try:
            key = decrypt_secret(item.encrypted_api_key)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                400, "Не удалось прочитать сохранённый API ключ. Введите ключ заново на этом компьютере.",
            ) from exc
    if not key and not is_local_url(base):
        raise HTTPException(400, "API ключ не задан")
    return base, key or ""


@router.post("/model/models")
async def list_model_models(
    payload: ModelModelsIn, request: Request, db: Session = Depends(get_db)
):
    _check_model_origin(request)
    try:
        item = db.get(AIModelSettings, 1)
        base, key = _model_connection(payload, item)
        return _no_store({"models": await _models(base, key)})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "Не удалось получить список моделей") from exc


@router.put("/model/settings")
async def save_model_settings(
    payload: ModelSettingsIn, request: Request, db: Session = Depends(get_db)
):
    _check_model_origin(request)
    try:
        item = db.get(AIModelSettings, 1)
        base, key = _model_connection(payload, item)
        await ModelGateway(provider="openai_compat").check_connection(base, key, payload.model)
        if item is None:
            item = AIModelSettings(
                id=1, base_url=base, model=payload.model, encrypted_api_key=encrypt_secret(key) if key else ""
            )
            db.add(item)
        else:
            if payload.api_key or normalize_base_url(item.base_url) != base:
                item.encrypted_api_key = encrypt_secret(key) if key else ""
            item.base_url, item.model = base, payload.model
        db.commit()
        return get_model_settings(db)
    except HTTPException:
        raise
    except ModelUnavailable as exc:
        db.rollback()
        raise HTTPException(400, "Проверка генерации не пройдена. Проверьте адрес, ключ, имя модели и поддержку Chat Completions. " + str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, "Не удалось сохранить настройки модели") from exc


def notification_dict(item: Notification) -> dict:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "target_path": item.target_path,
        "kind": item.kind,
        "title": item.title,
        "message": item.message,
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("/notifications")
def notifications(
    limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)
) -> list[dict]:
    items = db.scalars(
        select(Notification)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    ).all()
    return [notification_dict(item) for item in items]


@router.patch("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Notification, notification_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    if item.read_at is None:
        item.read_at = datetime.now(timezone.utc)
        db.commit()
    return notification_dict(item)


@router.post("/notifications/{notification_id}/read", include_in_schema=False)
def mark_notification_read_legacy(notification_id: int, db: Session = Depends(get_db)) -> dict:
    return mark_notification_read(notification_id, db)


@router.post("/notifications/read-all")
def mark_all_notifications_read(db: Session = Depends(get_db)) -> dict:
    result = db.execute(
        update(Notification)
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return {"updated": result.rowcount}


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desired_job_description: str = Field(default="", max_length=2000)
    minimum_scores: dict[str, int | bool] | None = None
    adapter_id: str
    application_limit: int | None = Field(default=5, ge=1)
    guaranteed_application: bool = False
    hirehi_pro_enabled: bool = False
    cover_letter_auto: bool = True
    cover_letter_template: str = Field(default="", max_length=12000)
    # ``None`` means that the cover-letter writer uses its default cap.
    cover_letter_max_words: int | None = Field(default=150, ge=1, le=10000)
    auto_start: bool = False

    @classmethod
    def _minimum_limits(cls) -> dict[str, int]:
        return {
            "tasks": 4,
            "skills": 2,
            "experience_depth": 4,
            "role_match": 4,
            "industry": 4,
            "special_requirements": 2,
        }

    @model_validator(mode="after")
    def validate_minimum_scores(self) -> SessionCreate:
        # Store one canonical primary-score gate map for the workflow.
        # Special requirements are fixed at 1.
        defaults = {"tasks": 2, "skills": 1, "experience_depth": 1, "role_match": 1,
                    "industry": 2, "special_requirements": 1}
        supplied = dict(self.minimum_scores or {})
        forbidden = {"work_conditions", "special_requirements", "title", "required_years", "languages"} & set(supplied)
        unknown_forbidden = forbidden - {"special_requirements"}
        if unknown_forbidden:
            raise ValueError(f"Неизвестный критерий минимального балла: {sorted(unknown_forbidden)[0]}")
        if "special_requirements" in supplied and supplied["special_requirements"] != 1:
            raise ValueError("Минимум special_requirements всегда равен 1")
        for key in ("tasks", "industry", "skills", "experience_depth", "role_match"):
            maximum = 2 if key == "skills" else 4
            if key in supplied and (isinstance(supplied[key], bool) or supplied[key] not in range(1, maximum + 1)):
                raise ValueError(f"Минимум {key} должен быть от 1 до {maximum}")
        supplied = {**defaults, **supplied}
        self.minimum_scores = supplied
        for key, value in (self.minimum_scores or {}).items():
            maximum = self._minimum_limits().get(key)
            if maximum is None:
                raise ValueError(f"Неизвестный критерий минимального балла: {key}")
            if isinstance(value, bool) or not 0 <= value <= maximum:
                raise ValueError(f"Минимум {key} должен быть от 0 до {maximum}")
        return self

    @model_validator(mode="after")
    def validate_cover_letter(self) -> SessionCreate:
        if not self.cover_letter_auto and not self.cover_letter_template.strip():
            raise ValueError("Укажите структуру сопроводительного письма или включите автоматическое написание")
        return self


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@router.get("/model/status")
@router.post("/model/check")
async def model_status(db: Session = Depends(get_db)):
    """Return catalog availability and observed generation health separately.

    Catalog discovery is a provider check.  Generation health is a read-only
    summary of real broker work and must never be inferred from that catalog
    response.  Diagnostic ids are deliberately exposed instead of internal
    request ids or provider error details.
    """
    catalog = await ModelGateway().status()
    session_factory = sessionmaker(
        bind=db.get_bind(), autoflush=False, expire_on_commit=False
    )
    health = ModelRequestBroker(session_factory).generation_health()

    def event(request_id: str | None, happened_at: datetime | None) -> dict | None:
        if request_id is None or happened_at is None:
            return None
        request_row = db.get(ModelRequest, request_id)
        timestamp = (
            happened_at
            if happened_at.tzinfo is not None
            else happened_at.replace(tzinfo=timezone.utc)
        )
        return {
            "diagnostic_id": request_row.diagnostic_id if request_row is not None else None,
            "at": timestamp.isoformat(),
        }

    generation_health = {
        "healthy": health["healthy"],
        "success_count": health["success_count"],
        "failure_count": health["failure_count"],
        "running": health["running"],
        "queued": health["queued"],
        "last_success": event(
            health.get("last_success_request_id"), health.get("last_success_at")
        ),
        "last_failure": event(
            health.get("last_failure_request_id"), health.get("last_failure_at")
        ),
    }
    return _no_store({**catalog, "generation_health": generation_health})


class ResumeSourcePreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: str = Field(min_length=1, max_length=50)
    resume_url: str = Field(min_length=1, max_length=2048)


class ResumeSourceConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: str = Field(min_length=1, max_length=50)
    preview_token: str = Field(min_length=20, max_length=256)
    consent: Literal[True]
    grammatical_gender: Literal["male", "female"] | None = None


class ResumeSourceGenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grammatical_gender: Literal["male", "female"]


@router.post("/resume-sources/preview")
async def preview_resume_source(payload: ResumeSourcePreviewIn, db: Session = Depends(get_db)) -> dict:
    """Read a site resume and issue a short-lived, one-use preview token.

    The response intentionally contains only structural coverage metadata. The
    canonical public URL is retained in the preview state for revalidation,
    but preview responses do not need to return it; private values remain
    server-side until local letter/form filling.
    """
    lock = _profile_operation_lock(payload.adapter_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Профиль сайта занят другой операцией")
    try:
        try:
            _assert_durable_profile_free(db, adapter_registry.get(payload.adapter_id).site_id)
        except KeyError as exc:
            raise HTTPException(400, "Неизвестный сайт вакансий") from exc
        try:
            canonical_url, ref = validate_adapter_resume_url(payload.adapter_id, payload.resume_url)
            snapshot = await extract_resume(
                payload.adapter_id, canonical_url, validated=(canonical_url, ref)
            )
            token = issue_preview_token(
                db, payload.adapter_id, snapshot, source_url=canonical_url
            )
        except KeyError as exc:
            raise HTTPException(400, "Неизвестный сайт вакансий") from exc
        except ResumeImportUnavailable as exc:
            raise HTTPException(501, str(exc)) from exc
        except ResumeImportError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
    finally:
        lock.release()
    return _no_store({"preview_token": token, "preview": public_preview(snapshot)})


@router.post("/resume-sources/confirm")
def confirm_resume_source(
    payload: ResumeSourceConfirmIn, db: Session = Depends(get_db)
) -> dict:
    """Save a confirmed link without consuming its launch preview token."""
    try:
        adapter_registry.get(payload.adapter_id)
    except KeyError as exc:
        raise HTTPException(400, "Неизвестный сайт вакансий") from exc
    try:
        row, token = confirm_saved_resume_source(
            db,
            adapter_id=payload.adapter_id,
            preview_token=payload.preview_token,
            consent=payload.consent,
            grammatical_gender=payload.grammatical_gender,
        )
    except ResumeImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _no_store(saved_resume_source_record(row, preview_token=token))


@router.patch("/resume-sources/{adapter_id}")
def update_resume_source_gender(
    adapter_id: str, payload: ResumeSourceGenderIn, db: Session = Depends(get_db)
) -> dict:
    """Change the local grammatical-gender preference for one source."""
    try:
        adapter_registry.get(adapter_id)
    except KeyError as exc:
        raise HTTPException(400, "Неизвестный сайт вакансий") from exc
    try:
        row = update_saved_resume_gender(db, adapter_id, payload.grammatical_gender)
    except SavedResumeSourceNotFound as exc:
        raise HTTPException(404, "Сохраненный источник не найден") from exc
    except ResumeImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _no_store(saved_resume_source_record(row))


@router.get("/resume-sources")
async def saved_resume_sources(db: Session = Depends(get_db)) -> list[dict]:
    """List confirmed sources from durable storage without network access."""
    records = await list_saved_resume_sources(db)
    return JSONResponse(
        [saved_resume_source_record(row, preview_token=token) for row, token in records],
        headers={"Cache-Control": "no-store"},
    )


@router.post("/resume-sources/{adapter_id}/refresh")
async def refresh_resume_source(adapter_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        adapter_registry.get(adapter_id)
    except KeyError as exc:
        raise HTTPException(400, "Неизвестный сайт вакансий") from exc
    lock = _profile_operation_lock(adapter_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Профиль сайта занят другой операцией")
    try:
        _assert_durable_profile_free(db, adapter_registry.get(adapter_id).site_id)
        try:
            row, token = await refresh_saved_resume_source(
                db,
                adapter_id,
                issue_token=not uses_saved_resume_data(adapter_id),
            )
        except SavedResumeSourceNotFound as exc:
            raise HTTPException(404, "Сохраненный источник не найден") from exc
        return _no_store(saved_resume_source_record(row, preview_token=token))
    finally:
        lock.release()


@router.delete("/resume-sources/{adapter_id}")
def delete_resume_source(adapter_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        adapter_registry.get(adapter_id)
    except KeyError as exc:
        raise HTTPException(400, "Неизвестный сайт вакансий") from exc
    try:
        delete_saved_resume_source(db, adapter_id)
    except SavedResumeSourceNotFound as exc:
        raise HTTPException(404, "Сохраненный источник не найден") from exc
    return _no_store({"ok": True, "adapter_id": adapter_id})


@router.api_route(
    "/policies",
    methods=["GET", "POST"],
    include_in_schema=False,
)
@router.api_route(
    "/policies/compile",
    methods=["GET", "POST"],
    include_in_schema=False,
)
def removed_policy_api() -> None:
    raise HTTPException(404, "Policy API has been removed")


@router.get("/adapters")
def adapters() -> list[dict]:
    return adapter_registry.manifests()


def session_dict(item: JobSession) -> dict:
    result = {
        "id": item.id,
        "desired_job_description": getattr(item, "desired_job_description", ""),
        "minimum_scores": item.minimum_scores or None,
        "adapter_id": item.adapter_id,
        "application_limit": item.application_limit,
        "guaranteed_application": bool(item.guaranteed_application),
        "hirehi_pro_enabled": bool(getattr(item, "hirehi_pro_enabled", False)),
        "cover_letter_auto": getattr(item, "cover_letter_auto", None) is not False,
        "cover_letter_template": getattr(item, "cover_letter_template", "") or "",
        "cover_letter_max_words": getattr(item, "cover_letter_max_words", None),
        "auto_start": bool(getattr(item, "auto_start", True)),
        "status": item.status,
        "counters": item.counters or {},
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        "stop_reason": item.stop_reason,
    }
    # Expose only non-sensitive snapshot metadata. The raw source URL and
    # private view never leave the local backend.
    snapshot = getattr(item, "resume_snapshot", None)
    if snapshot is None:
        # Avoid an ORM relationship (and accidental eager loading) on legacy
        # models; this is populated by the dedicated endpoint when needed.
        result["resume_snapshot"] = None
    else:
        result["resume_snapshot"] = {
            "source_site": snapshot.source_site,
            "imported_at": snapshot.imported_at.isoformat(),
        }
    return result


@router.post("/sessions", status_code=202)
def create_session(
    payload: SessionCreate,
    db: Session = Depends(get_db),
    request: Request = None,
) -> dict:
    # The idempotency and site-lease rows form one small critical section.
    # This protects the common two-client race before the database's unique
    # constraints are reached (and keeps a losing request from creating a
    # transient JobSession at all).
    with _session_create_lock:
        return _create_session(payload, db, request)


def _create_session(
    payload: SessionCreate, db: Session, request: Request | None = None
) -> dict:
    # Keep direct service callers usable while FastAPI supplies Request/DB.
    # Persist clean launch/template copies; the submitted request object stays
    # untouched for validation and audit, and injected prose cannot alter the
    # workflow's trusted instructions.
    safe_description = sanitize_untrusted_input(
        payload.desired_job_description, context="candidate search preferences"
    )
    safe_template = sanitize_untrusted_input(
        payload.cover_letter_template, context="candidate letter template"
    )
    try:
        adapter_registry.get(payload.adapter_id)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Durable runtime path: cache-backed sites pin their confirmed local copy
    # here. Check idempotency before the mutable saved-source row so a retry
    # keeps its original ID even if the profile was edited between requests.
    key = (request.headers.get("Idempotency-Key") if request is not None else "") or ""
    key = key.strip()
    payload_hash = canonical_payload_hash(payload.model_dump(mode="json"))
    if len(key) > 255:
        raise HTTPException(400, "Idempotency-Key too long")
    if key:
        prior = find_idempotency(db, key)
        if prior is not None:
            if prior.payload_hash != payload_hash:
                raise HTTPException(409, "Idempotency-Key payload mismatch")
            existing = db.get(JobSession, prior.session_id)
            if existing is None:
                raise HTTPException(409, "Idempotency-Key target is unavailable")
            return session_dict(existing)
    # Launches always come from a confirmed durable source. Preview tokens are
    # intentionally limited to the confirmation flow and cannot launch a
    # session on their own.
    saved_source = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == payload.adapter_id)
    )
    if saved_source is None:
        if uses_saved_resume_data(payload.adapter_id):
            raise HTTPException(
                400, _saved_resume_launch_error(payload.adapter_id, missing=True)
            )
        raise HTTPException(400, "Сначала проверьте и подтвердите ссылку на резюме выбранного сайта")
    # Cache-backed sessions are pinned synchronously to the last locally
    # confirmed full snapshot. This happens before a JobSession or lease is
    # created, so a missing/corrupt cache cannot leave accepted work behind.
    saved_snapshot = None
    if uses_saved_resume_data(payload.adapter_id):
        try:
            saved_snapshot = load_saved_resume_data(saved_source)
        except ResumeImportError as exc:
            raise HTTPException(
                400, _saved_resume_launch_error(payload.adapter_id, missing=False)
            ) from exc
    site_id = adapter_registry.get(payload.adapter_id).site_id
    current_lease = db.get(SiteExecutionLease, site_id)
    if current_lease is not None:
        owner = db.get(JobSession, current_lease.session_id)
        if owner is None or owner.status in {
            SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.FAILED, "CANCELLED",
        }:
            db.delete(current_lease)
            db.flush()
        else:
            raise HTTPException(409, "Another session owns this site")
    item = JobSession(
        desired_job_description=safe_description,
        minimum_scores=payload.minimum_scores or None,
        adapter_id=payload.adapter_id,
        application_limit=None if payload.adapter_id == "hirehi" else payload.application_limit,
        guaranteed_application=payload.guaranteed_application,
        hirehi_pro_enabled=payload.hirehi_pro_enabled if payload.adapter_id == "hirehi" else False,
        cover_letter_auto=payload.cover_letter_auto,
        cover_letter_template=safe_template,
        cover_letter_max_words=payload.cover_letter_max_words,
        status=SessionStatus.PREPARING,
        counters={},
    )
    db.add(item)
    db.flush()
    if saved_snapshot is not None:
        try:
            persisted_snapshot = persist_session_snapshot(db, item.id, saved_snapshot)
        except ResumeImportError as exc:
            db.rollback()
            raise HTTPException(400, _SAVED_RESUME_REFRESH_ERROR) from exc
    else:
        persisted_snapshot = None
    ensure_execution(
        db, item.id, stage="PREPARING",
        source_url=getattr(saved_source, "source_url", None),
        source_url_hash=(persisted_snapshot.source_url_hash if persisted_snapshot else
                         getattr(saved_source, "source_url_hash", None)),
        source_content_hash=(
            persisted_snapshot.content_hash
            if persisted_snapshot
            else (
                getattr(saved_source, "content_hash", None)
                if uses_saved_resume_data(payload.adapter_id)
                else None
            )
        ),
    )
    if not claim_site_lease(db, site_id, item.id, 0):
        db.rollback()
        raise HTTPException(409, "Another session owns this site")
    if payload.auto_start:
        request_start(db, item.id)
    if key:
        try:
            idempotency_record(db, key, payload_hash, item.id)
        except IntegrityError:
            db.rollback()
            prior = find_idempotency(db, key)
            if prior is None or prior.payload_hash != payload_hash:
                raise HTTPException(409, "Idempotency-Key conflict") from None
            existing = db.get(JobSession, prior.session_id)
            return session_dict(existing)
    db.commit()
    db.refresh(item)
    if runtime_supervisor.start(site_id=site_id, session_id=item.id) is None:
        # A supervisor-level conflict can happen after the transaction (for
        # example, an old process is still being retired).  Do not leave an
        # unstartable accepted row or an idempotency key behind.
        db.query(SessionIdempotencyKey).filter(
            SessionIdempotencyKey.session_id == item.id
        ).delete(synchronize_session=False)
        db.delete(item)
        db.commit()
        raise HTTPException(409, "Another session owns this site")
    return session_dict(item)
    """
    try:
        refreshed, launch_snapshot = (None, None)  # unreachable legacy text
            # importer moved to backend.runtime.worker
        )
        if refreshed.status == "unavailable":
            raise ResumeImportError("Не удалось повторно проверить сохраненное резюме")
    except ResumeImportError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    item = JobSession(
        desired_job_description=safe_description,
        minimum_scores=payload.minimum_scores or None,
        adapter_id=payload.adapter_id,
        # HireHi discovery sessions are stopped manually and therefore never
        # carry a numeric application cap, including requests from legacy or
        # third-party clients that still submit one.
        application_limit=None if payload.adapter_id == "hirehi" else payload.application_limit,
        guaranteed_application=payload.guaranteed_application,
        # PRO tools are specific to HireHi; normalize the value for all other
        # adapters so clients can safely send a shared launch payload.
        hirehi_pro_enabled=payload.hirehi_pro_enabled if payload.adapter_id == "hirehi" else False,
        cover_letter_auto=payload.cover_letter_auto,
        cover_letter_template=safe_template,
        cover_letter_max_words=payload.cover_letter_max_words,
        status=SessionStatus.CREATED,
        counters={},
    )
    db.add(item)
    db.flush()
    try:
        # snapshot persistence moved to backend.runtime.worker
    except ResumeImportError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    db.refresh(item)
    return session_dict(item)
    """


def _session_list_item(row) -> dict:
    """Small history projection; deliberately excludes recovery JSON."""
    return {
        "id": row.id,
        "desired_job_description": row.desired_job_description or "",
        "minimum_scores": row.minimum_scores,
        "adapter_id": row.adapter_id,
        "application_limit": row.application_limit,
        "guaranteed_application": bool(row.guaranteed_application),
        "hirehi_pro_enabled": bool(row.hirehi_pro_enabled),
        "cover_letter_auto": row.cover_letter_auto is not False,
        "cover_letter_template": row.cover_letter_template or "",
        "cover_letter_max_words": row.cover_letter_max_words,
        "auto_start": True,
        "status": row.status,
        "counters": row.counters or {},
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "stop_reason": row.stop_reason,
        "execution_stage": row.execution_stage,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "resume_snapshot": None,
    }


def _session_history_page(
    db: Session, *, limit: int, offset: int, terminal_only: bool = False
) -> dict:
    if limit < 1 or limit > 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(422, "offset must be non-negative")
    terminal_statuses = (
        SessionStatus.COMPLETED,
        SessionStatus.STOPPED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    )
    filters = [JobSession.status.in_(terminal_statuses)] if terminal_only else []
    total = db.scalar(select(func.count(JobSession.id)).where(*filters)) or 0
    stmt = (
        select(
            JobSession.id, JobSession.desired_job_description, JobSession.minimum_scores,
            JobSession.adapter_id, JobSession.application_limit,
            JobSession.guaranteed_application, JobSession.hirehi_pro_enabled,
            JobSession.cover_letter_auto, JobSession.cover_letter_template,
            JobSession.cover_letter_max_words, JobSession.status, JobSession.counters,
            JobSession.started_at, JobSession.finished_at, JobSession.stop_reason,
            SessionExecution.stage.label("execution_stage"),
            SessionExecution.heartbeat_at,
        )
        .outerjoin(SessionExecution, SessionExecution.session_id == JobSession.id)
        .where(*filters)
        .order_by(JobSession.id.desc())
        .limit(limit).offset(offset)
    )
    items = [_session_list_item(row) for row in db.execute(stmt)]
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(items) < total}


@router.get("/sessions")
def sessions(
    legacy: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[dict]:
    # The historical endpoint is intentionally a light list for old clients.
    page = _session_history_page(db, limit=50, offset=0)
    return page["items"]


@router.get("/sessions/history")
def sessions_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    terminal_only: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return _no_store(
        _session_history_page(db, limit=limit, offset=offset, terminal_only=terminal_only)
    )


@router.get("/sessions/{session_id}/resume")
def session_resume(session_id: int, db: Session = Depends(get_db)) -> dict:
    """Return only a read-only, non-PII resume summary for the session."""
    item = db.get(JobSession, session_id)
    if item is None:
        raise HTTPException(404, "Сессия не найдена")
    snapshot = db.scalar(
        select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
    )
    if snapshot is None:
        raise HTTPException(404, "Временный снимок резюме недоступен")
    safe = SiteResumeSnapshot.model_validate(snapshot.snapshot)
    execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
    result = public_preview(safe, source_url=getattr(execution, "source_url", None) if execution else None)
    result["session_id"] = session_id
    result["professional"] = snapshot.professional_view
    private = _unseal_private(snapshot.private_view)
    identity = private.get("identity", {})
    contacts = private.get("contacts", {})
    result["private_fields_found"] = {
        "full_name": isinstance(identity, dict) and isinstance(identity.get("full_name"), dict)
        and identity["full_name"].get("availability") == "present",
        "phone": isinstance(contacts, dict) and isinstance(contacts.get("phone"), dict)
        and contacts["phone"].get("availability") == "present",
        "email": isinstance(contacts, dict) and isinstance(contacts.get("email"), dict)
        and contacts["email"].get("availability") == "present",
    }
    result["snapshot"] = snapshot.full_snapshot or safe.model_dump(mode="json")
    # Explicitly avoid returning the encrypted private payload as well.
    return _no_store(result)


@router.get("/sessions/{session_id}/ai-context")
def session_ai_context(session_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    item = db.get(JobSession, session_id)
    if item is None:
        raise HTTPException(404, "Session not found")
    snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id))
    if snapshot is None:
        raise HTTPException(404, "Resume snapshot unavailable")
    from backend.services.resume_session import full_resume_model_payload
    model_resume = full_resume_model_payload(snapshot.full_snapshot or snapshot.snapshot)
    # The model-facing projection is complete and excludes the sealed private
    # payload; no-store prevents browser caches retaining resume context.
    return _no_store({
        "session_id": session_id,
        "resume": model_resume,
        "snapshot": snapshot.full_snapshot or snapshot.snapshot,
    })


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    if item.status not in {SessionStatus.PREPARING, SessionStatus.CREATED}:
        raise HTTPException(409, "Запустить можно только новую сессию")
    snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id))
    saved_source = None
    if uses_saved_resume_data(item.adapter_id):
        saved_source = db.scalar(
            select(SavedResumeSource).where(SavedResumeSource.adapter_id == item.adapter_id)
        )
        if saved_source is None:
            raise HTTPException(
                422, _saved_resume_launch_error(item.adapter_id, missing=True)
            )
        try:
            # A launch requires the profile's current saved copy to remain
            # usable. The session still runs from its own pinned snapshot when
            # one exists; this check never replaces that immutable copy.
            load_saved_resume_data(saved_source)
        except ResumeImportError as exc:
            raise HTTPException(
                422, _saved_resume_launch_error(item.adapter_id, missing=False)
            ) from exc
    if snapshot is not None:
        _validate_session_resume_snapshot(snapshot, item.adapter_id)
        if _private_gender_value(snapshot.private_view) is None:
            raise HTTPException(422, "Resume snapshot requires a valid gender")
    elif uses_saved_resume_data(item.adapter_id):
        # Older PREPARING sessions were created before the API pinned their
        # local source copy.  Backfill once from disk/database, never from the
        # public site, before requesting the worker start.
        try:
            # saved_source was checked above, before any snapshot or lease
            # mutation, so this is also the source for one-time legacy repair.
            full = load_saved_resume_data(saved_source)
            snapshot = persist_session_snapshot(db, session_id, full)
        except ResumeImportError as exc:
            db.rollback()
            raise HTTPException(
                422, _saved_resume_launch_error(item.adapter_id, missing=False)
            ) from exc
        _validate_session_resume_snapshot(snapshot, item.adapter_id)
        ensure_execution(
            db,
            session_id,
            source_url_hash=snapshot.source_url_hash,
            source_content_hash=snapshot.content_hash,
        )
    elif item.status == SessionStatus.CREATED:
        raise HTTPException(400, "Временный снимок резюме не найден; проверьте ссылку ещё раз")
    if item.status == SessionStatus.CREATED:
        ensure_execution(
            db,
            session_id,
            source_url_hash=snapshot.source_url_hash,
            source_content_hash=snapshot.content_hash,
        )
    item.status = SessionStatus.PREPARING
    request_start(db, session_id)
    db.commit()
    adapter = adapter_registry.get(item.adapter_id)
    if runtime_supervisor.start(site_id=adapter.site_id, session_id=session_id) is None:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    return session_dict(item)


@router.post("/sessions/{session_id}/pause")
def pause_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(
        409, "Ручная пауза отключена; сессию можно приостановить только при CAPTCHA"
    )


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    if item.status != SessionStatus.PAUSED:
        raise HTTPException(409, "Продолжить можно только приостановленную сессию")
    snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id))
    if snapshot is None:
        raise HTTPException(400, "Временный снимок резюме не найден; восстановление невозможно")
    # A PAUSED session resumes its already checked immutable snapshot.
    _validate_session_resume_snapshot(snapshot, item.adapter_id)
    ensure_execution(
        db,
        session_id,
        source_url_hash=snapshot.source_url_hash,
        source_content_hash=snapshot.content_hash,
    )
    request_start(db, session_id)
    item.status = SessionStatus.PREPARING
    item.stop_reason = None
    db.commit()
    adapter = adapter_registry.get(item.adapter_id)
    if runtime_supervisor.start(site_id=adapter.site_id, session_id=session_id) is None:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    return session_dict(item)


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    durable_db = all(hasattr(db, name) for name in ("add", "flush", "scalar", "commit"))
    if not durable_db:
        item.status = SessionStatus.STOPPED
        item.stop_reason = "Остановлено пользователем"
        item.finished_at = datetime.now(timezone.utc)
        if item.adapter_id == "hirehi":
            workflow_manager.write_hirehi_report(session_id)
        await close_browser(session_id)
        return session_dict(item)

    if item.status in {
        SessionStatus.COMPLETED,
        SessionStatus.CANCELLED,
        SessionStatus.STOPPED,
        SessionStatus.FAILED,
    }:
        try:
            site_id = adapter_registry.get(item.adapter_id).site_id
        except KeyError:
            site_id = item.adapter_id
        await close_browser(session_id)
        release_browser_lease(session_id, site_id)
        release_site_lease(db, site_id, session_id)
        db.commit()
        return session_dict(item)

    request_cancel(db, session_id, reason="user")
    db.commit()
    try:
        adapter = adapter_registry.get(item.adapter_id)
        site_id = adapter.site_id
    except KeyError:
        # Historical/imported sessions can outlive an adapter registration;
        # the durable site lease is keyed by the stored adapter identifier.
        site_id = item.adapter_id

    # A live process worker owns its own browser and terminal cleanup. Its
    # monitor observes STOPPED/COMPLETED IPC and performs the CANCELLED
    # transition asynchronously, so the API must return the durable STOPPING
    # intent without waiting for browser shutdown.
    if runtime_supervisor.cancel(site_id=site_id, session_id=session_id):
        return session_dict(item)

    # Compatibility path for an in-process workflow task or a paused session
    # with no worker. The durable cancel fence is already visible before this
    # task is interrupted, preventing another form submission from starting.
    manager = workflow_manager
    task = manager.tasks.get(session_id)
    task_was_running = bool(task and not task.done())
    if task_was_running:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    await close_browser(session_id)
    release_browser_lease(session_id, site_id)
    db.refresh(item)

    # Report generation precedes terminalizing pending vacancies to preserve
    # the existing HireHi contract for both paused sessions and legacy tasks.
    if item.adapter_id == "hirehi":
        manager.write_hirehi_report(session_id)
    item.status = SessionStatus.CANCELLED
    item.stop_reason = "Остановлено пользователем"
    item.finished_at = datetime.now(timezone.utc)
    manager._terminalize_pending_vacancies(db, item)
    from backend.orchestrator.workflow import _scrub_snapshot_question_artifacts

    _scrub_snapshot_question_artifacts(db, session_id)
    execution = db.scalar(
        select(SessionExecution).where(SessionExecution.session_id == session_id)
    )
    if execution is not None:
        execution.stage = "CANCELLED"
        execution.cancel_requested = True
        execution.last_progress_at = datetime.now(timezone.utc)
        execution.wait_reason = "user"
    release_site_lease(db, site_id, session_id)
    db.commit()
    if (getattr(item, "recovery", None) or {}).get("measurement_identity"):
        from backend.services.search_metrics import freeze

        db.refresh(item)
        freeze(db, item)
    db.commit()
    return session_dict(item)


@router.post("/sessions/{session_id}/browser")
async def open_session_browser(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    adapter = adapter_registry.get(item.adapter_id)
    worker = runtime_supervisor.worker(adapter.site_id)
    if worker is not None:
        if worker.session_id != session_id:
            raise HTTPException(409, "Браузер сайта принадлежит другой рабочей сессии")
        if not worker.process.is_alive():
            raise HTTPException(409, "Рабочая сессия браузера восстанавливается; повторите попытку позже")
        result = await runtime_supervisor.open_browser(site_id=adapter.site_id, session_id=session_id)
        if not result.get("ok"):
            raise HTTPException(503, result.get("message") or "Рабочая сессия не открыла браузер")
        return result
    if item.status in {SessionStatus.PREPARING, SessionStatus.RUNNING, SessionStatus.STOPPING}:
        raise HTTPException(409, "Активная сессия ожидает восстановления рабочего браузера")
    existing = get_browser(session_id)
    if existing:
        return {"ok": True, "message": "Браузер уже открыт"}
    durable_db = hasattr(db, "add") and hasattr(db, "commit")
    if durable_db:
        execution = ensure_execution(db, session_id)
        if not claim_site_lease(db, adapter.site_id, session_id, execution.generation):
            raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
        db.commit()
    if not acquire_browser_lease(session_id, adapter.site_id):
        if durable_db:
            release_site_lease(db, adapter.site_id, session_id)
            db.commit()
        raise HTTPException(409, "Для этого сайта уже открыт браузер другой сессии")
    executor = BrowserExecutor(adapter.site_id, adapter.allowed_domains, headless=False)
    try:
        await executor.start()
    except Exception:
        release_browser_lease(session_id, adapter.site_id)
        if durable_db:
            release_site_lease(db, adapter.site_id, session_id)
            db.commit()
        raise
    # Keep the landing page explicit per supported site: this is also the
    # first page used for manual login in the persistent Chromium profile.
    targets = {"hh": "https://hh.ru/", "hirehi": "https://hirehi.ru/", "zarplata": "https://zarplata.ru/"}
    target = targets.get(item.adapter_id, f"https://{adapter.allowed_domains[0]}/")
    site_name = getattr(adapter, "display_name", item.adapter_id)
    set_browser(session_id, executor)
    try:
        await executor.execute("navigate", url=target)
    except Exception:
        return {
            "ok": True,
            "message": (
                f"Chromium открыт. Автопереход на {site_name} не завершился; "
                f"при необходимости откройте {target} вручную."
            ),
        }
    return {
        "ok": True,
        "message": f"Открыт отдельный постоянный профиль Chromium для входа в {site_name}",
    }


@router.post("/sessions/{session_id}/browser/check")
async def check_session_browser(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    adapter = adapter_registry.get(item.adapter_id)
    worker = runtime_supervisor.worker(adapter.site_id)
    if worker is not None:
        if worker.session_id != session_id:
            raise HTTPException(409, "Браузер сайта принадлежит другой рабочей сессии")
        if not worker.process.is_alive():
            raise HTTPException(409, "Рабочая сессия браузера восстанавливается; повторите попытку позже")
        login = await runtime_supervisor.check_login(site_id=adapter.site_id, session_id=session_id)
        if not login.get("ok"):
            raise HTTPException(503, login.get("message") or "Не удалось проверить вход в браузере сессии")
        if not login.get("authenticated"):
            raise HTTPException(400, login.get("message") or "Войдите в аккаунт в открытом браузере")
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        if execution is not None and not execution.start_requested:
            request_start(db, session_id)
            item.status = SessionStatus.PREPARING
            db.commit()
            if runtime_supervisor.start(site_id=adapter.site_id, session_id=session_id) is None:
                raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
        return {
            "ok": True,
            "message": f"Вход в {getattr(adapter, 'display_name', item.adapter_id)} подтверждён в браузере сессии",
        }
    else:
        if item.status in {SessionStatus.PREPARING, SessionStatus.RUNNING, SessionStatus.STOPPING}:
            raise HTTPException(409, "Вход проверяется рабочей сессией автоматически")
        executor = get_browser(session_id)
        if not executor:
            raise HTTPException(
                400,
                f"Сначала откройте Chromium для {getattr(adapter, 'display_name', item.adapter_id)}",
            )
        login = await adapter.get_login_state(executor.page)
        if not login.authenticated:
            raise HTTPException(400, login.message)
        # Playwright contexts cannot be transferred between API and worker
        # processes. Close the manual login window before the worker opens the
        # same persistent profile, so two contexts never own it at once.
        await close_browser(session_id)
        release_browser_lease(session_id, adapter.site_id)
    request_start(db, session_id)
    item.status = SessionStatus.PREPARING
    db.commit()
    if runtime_supervisor.start(site_id=adapter.site_id, session_id=session_id) is None:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    return {
        "ok": True,
        "message": f"Вход в {getattr(adapter, 'display_name', item.adapter_id)} подтверждён, сессия продолжена",
    }


@router.get("/sessions/{session_id}/browser/login-status")
async def session_browser_login_status(session_id: int, db: Session = Depends(get_db)) -> dict:
    """Return login state without starting or mutating the workflow."""
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    adapter = adapter_registry.get(item.adapter_id)
    worker = runtime_supervisor.worker(adapter.site_id)
    if worker is not None:
        if worker.session_id != session_id:
            raise HTTPException(409, "Браузер сайта принадлежит другой рабочей сессии")
        if not worker.process.is_alive():
            raise HTTPException(409, "Рабочая сессия браузера восстанавливается; повторите попытку позже")
        result = await runtime_supervisor.check_login(site_id=adapter.site_id, session_id=session_id)
        if not result.get("ok"):
            raise HTTPException(503, result.get("message") or "Не удалось проверить браузер сессии")
        return {
            "authenticated": bool(result.get("authenticated")),
            "message": str(result.get("message", ""))[:255],
            "url": result.get("url"),
        }
    if item.status in {SessionStatus.PREPARING, SessionStatus.RUNNING, SessionStatus.STOPPING}:
        return {
            "authenticated": False,
            "message": "Вход проверяется рабочей сессией автоматически",
            "url": None,
        }
    executor = get_browser(session_id)
    if not executor:
        raise HTTPException(
            400,
            f"Сначала откройте Chromium для {getattr(adapter, 'display_name', item.adapter_id)}",
        )

    login = await adapter.get_login_state(executor.page)
    raw_url = str(getattr(executor.page, "url", "") or "")
    parsed = urlparse(raw_url)
    try:
        validate_navigation = getattr(executor, "validate_navigation_url", None)
        if not callable(validate_navigation):
            raise ValueError("Текущий адрес браузера не удалось проверить")
        validate_navigation(raw_url)
        # Only the origin is useful to the UI.  Paths can contain opaque
        # resume IDs (bearer identifiers) and must never be echoed by this
        # status endpoint.
        safe_url = urlunparse((parsed.scheme.lower(), parsed.netloc, "/", "", "", ""))
    except (ValueError, TypeError):
        safe_url = None
    return {
        "authenticated": bool(login.authenticated),
        "message": str(login.message),
        "url": safe_url,
    }


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    result = session_dict(item)
    snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id))
    if snapshot is not None:
        execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == session_id))
        result["resume_snapshot"] = {
            "source_site": snapshot.source_site,
            "imported_at": snapshot.imported_at.isoformat(),
            "snapshot": snapshot.full_snapshot or snapshot.snapshot,
            "import_url": public_preview(
                SiteResumeSnapshot.model_validate(snapshot.snapshot),
                source_url=getattr(execution, "source_url", None) if execution else None,
            ).get("import_url"),
        }
    return _no_store(result)


@router.get("/sessions/{session_id}/report")
def session_report_status(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    if item.adapter_id != "hirehi":
        raise HTTPException(400, "PDF-отчёт доступен только для HireHi")
    path = Path("output/pdf") / f"hirehi-session-{session_id}.pdf"
    return {
        "ready": path.is_file(),
        "pdf_url": f"/api/sessions/{session_id}/report/pdf" if path.is_file() else None,
    }


@router.get("/sessions/{session_id}/report/pdf")
def session_report_pdf(session_id: int, db: Session = Depends(get_db)) -> FileResponse:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    if item.adapter_id != "hirehi":
        raise HTTPException(400, "PDF-отчёт доступен только для HireHi")
    path = Path("output/pdf") / f"hirehi-session-{session_id}.pdf"
    if not path.is_file():
        raise HTTPException(404, "PDF-отчёт ещё не готов")
    return FileResponse(
        path, media_type="application/pdf", filename=f"hirehi-session-{session_id}.pdf"
    )


@router.get("/sessions/{session_id}/metrics")
def session_metrics(session_id: int, db: Session = Depends(get_db)) -> dict:
    from backend.services.search_metrics import summary

    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    return (item.recovery or {}).get("measurement_report") or summary(db, item)


@router.get("/sessions/{session_id}/events")
def events(session_id: int, after: int = 0, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(
        select(BrowserEvent)
        .where(BrowserEvent.session_id == session_id, BrowserEvent.id > after)
        .order_by(BrowserEvent.id)
    )
    return [
        {
            "id": e.id,
            "type": e.event_type,
            "message": e.message,
            "data": e.data,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


def public_evaluation(data: dict | None) -> dict | None:
    if data is None:
        return None
    result = dict(data)
    result.pop("flag_matches", None)
    return result


async def session_socket(websocket: WebSocket, session_id: int) -> None:
    await websocket.accept()
    after = 0
    try:
        while True:
            from backend.persistence.database import SessionLocal

            with SessionLocal() as db:
                batch = list(
                    db.scalars(
                        select(BrowserEvent)
                        .where(BrowserEvent.session_id == session_id, BrowserEvent.id > after)
                        .order_by(BrowserEvent.id)
                    )
                )
                for event in batch:
                    await websocket.send_json(
                        {
                            "id": event.id,
                            "type": event.event_type,
                            "message": event.message,
                            "data": event.data,
                        }
                    )
                    after = event.id
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
