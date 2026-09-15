from __future__ import annotations

import asyncio
import csv
import io
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timezone
from pathlib import Path
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
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

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
from backend.intelligence.model_config import (
    auth_headers,
    is_local_url,
    model_http_client,
    normalize_base_url,
    validate_base_url,
)
from backend.intelligence.security import sanitize_untrusted_input
from backend.orchestrator.workflow import workflow_manager
from backend.persistence.crypto import decrypt_secret, encrypt_secret
from backend.persistence.database import get_db
from backend.persistence.models import (
    AIModelSettings,
    BrowserEvent,
    Evaluation,
    JobSession,
    Notification,
    SavedResumeSource,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.schemas.domain import (
    SessionStatus,
    SiteResumeSnapshot,
)
from backend.services.resume_session import (
    ResumeImportError,
    ResumeImportUnavailable,
    SavedResumeSourceNotFound,
    _unseal_private,
    confirm_saved_resume_source,
    delete_saved_resume_source,
    extract_resume,
    issue_preview_token,
    list_saved_resume_sources,
    persist_session_snapshot,
    public_preview,
    refresh_saved_resume_source,
    revalidate_saved_resume_source,
    saved_resume_source_record,
    update_saved_resume_gender,
    validate_adapter_resume_url,
)

router = APIRouter(prefix="/api")


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
    cover_letter_auto: bool = True
    cover_letter_template: str = Field(default="", max_length=12000)
    # ``None`` means that the cover-letter writer uses its default cap.
    cover_letter_max_words: int | None = Field(default=150, ge=1, le=10000)

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
async def model_status():
    return _no_store(await ModelGateway().status())


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
    try:
        row, token = await refresh_saved_resume_source(db, adapter_id)
    except SavedResumeSourceNotFound as exc:
        raise HTTPException(404, "Сохраненный источник не найден") from exc
    return _no_store(saved_resume_source_record(row, preview_token=token))


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
        "cover_letter_auto": getattr(item, "cover_letter_auto", None) is not False,
        "cover_letter_template": getattr(item, "cover_letter_template", "") or "",
        "cover_letter_max_words": getattr(item, "cover_letter_max_words", None),
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


@router.post("/sessions")
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> dict:
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
    # Launches always come from a confirmed durable source. Preview tokens are
    # intentionally limited to the confirmation flow and cannot launch a
    # session on their own.
    saved_source = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == payload.adapter_id)
    )
    if saved_source is None:
        raise HTTPException(400, "Сначала проверьте и подтвердите ссылку на резюме выбранного сайта")
    try:
        refreshed, launch_snapshot = asyncio.run(
            revalidate_saved_resume_source(db, payload.adapter_id)
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
        application_limit=payload.application_limit,
        guaranteed_application=payload.guaranteed_application,
        cover_letter_auto=payload.cover_letter_auto,
        cover_letter_template=safe_template,
        cover_letter_max_words=payload.cover_letter_max_words,
        status=SessionStatus.CREATED,
        counters={},
    )
    db.add(item)
    db.flush()
    try:
        persist_session_snapshot(db, item.id, launch_snapshot)
    except ResumeImportError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    db.refresh(item)
    return session_dict(item)


@router.get("/sessions")
def sessions(db: Session = Depends(get_db)) -> list[dict]:
    return [session_dict(s) for s in db.scalars(select(JobSession).order_by(JobSession.id.desc()))]


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
    result = public_preview(safe)
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
    # Explicitly avoid returning the encrypted private payload as well.
    return _no_store(result)


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    if item.status != SessionStatus.CREATED:
        raise HTTPException(409, "Запустить можно только новую сессию")
    snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id))
    if snapshot is None:
        raise HTTPException(400, "Временный снимок резюме не найден; проверьте ссылку ещё раз")
    # Creation already performs source revalidation. The immutable snapshot
    # is the only supported input for starting this session.
    if snapshot.source_site != item.adapter_id:
        raise HTTPException(409, "Снимок резюме принадлежит другому сайту")
    private = _unseal_private(snapshot.private_view)
    gender = (private.get("identity", {}).get("gender", {})
              if isinstance(private.get("identity"), dict) else {})
    source = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == item.adapter_id)
    )
    if (
        gender.get("value") not in {"male", "female"}
        and (source is None or source.grammatical_gender not in {"male", "female"})
    ):
        raise HTTPException(422, "Выберите мужской или женский род в настройках сохраненного резюме")
    if not item.cover_letter_auto and not (item.cover_letter_template or "").strip():
        raise HTTPException(422, "Укажите структуру сопроводительного письма")
    if workflow_manager.launch(session_id) is False:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    item.status = SessionStatus.RUNNING
    db.commit()
    return {"ok": True}


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
    if item.status not in {
        SessionStatus.PAUSED,
    }:
        raise HTTPException(409, "Продолжить можно только приостановленную сессию")
    snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id))
    if snapshot is None:
        raise HTTPException(400, "Временный снимок резюме не найден; восстановление невозможно")
    # A PAUSED session resumes its already checked immutable snapshot.
    if snapshot.source_site != item.adapter_id:
        raise HTTPException(409, "Снимок резюме принадлежит другому сайту")
    private = _unseal_private(snapshot.private_view)
    gender = (private.get("identity", {}).get("gender", {})
              if isinstance(private.get("identity"), dict) else {})
    source = db.scalar(
        select(SavedResumeSource).where(SavedResumeSource.adapter_id == item.adapter_id)
    )
    if (
        gender.get("value") not in {"male", "female"}
        and (source is None or source.grammatical_gender not in {"male", "female"})
    ):
        raise HTTPException(422, "Выберите мужской или женский род в настройках сохраненного резюме")
    if not item.cover_letter_auto and not (item.cover_letter_template or "").strip():
        raise HTTPException(422, "Укажите структуру сопроводительного письма")
    if workflow_manager.launch(session_id) is False:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    item.status = SessionStatus.RUNNING
    item.stop_reason = None
    db.commit()
    return session_dict(item)


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    item.status = SessionStatus.STOPPED
    item.stop_reason = "Остановлено пользователем"
    item.finished_at = datetime.now(timezone.utc)
    db.commit()
    task = workflow_manager.tasks.get(session_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await close_browser(session_id)
    if item.adapter_id == "hirehi":
        workflow_manager.write_hirehi_report(session_id)
    if (getattr(item, "recovery", None) or {}).get("measurement_identity"):
        # A CAPTCHA-paused session has no running task to freeze it in finally.
        from backend.services.search_metrics import freeze

        db.refresh(item)
        freeze(db, item)
    # STOPPED is terminal only after any report/metrics work above succeeds.
    from backend.services.resume_session import delete_snapshot

    delete_snapshot(db, session_id)
    db.commit()
    return session_dict(item)


@router.post("/sessions/{session_id}/browser")
async def open_session_browser(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    existing = get_browser(session_id)
    if existing:
        return {"ok": True, "message": "Браузер уже открыт"}
    adapter = adapter_registry.get(item.adapter_id)
    if not acquire_browser_lease(session_id, adapter.site_id):
        raise HTTPException(409, "Для этого сайта уже открыт браузер другой сессии")
    executor = BrowserExecutor(adapter.site_id, adapter.allowed_domains, headless=False)
    try:
        await executor.start()
    except Exception:
        release_browser_lease(session_id, adapter.site_id)
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
    executor = get_browser(session_id)
    if not executor:
        raise HTTPException(
            400,
            f"Сначала откройте Chromium для {getattr(adapter_registry.get(item.adapter_id), 'display_name', item.adapter_id)}",
        )
    adapter = adapter_registry.get(item.adapter_id)
    login = await adapter.get_login_state(executor.page)
    if not login.authenticated:
        raise HTTPException(400, login.message)
    if workflow_manager.launch(session_id) is False:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    item.status = SessionStatus.RUNNING
    item.stop_reason = None
    db.commit()
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
    return session_dict(item)


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


VACANCY_SCORE_KEYS = (
    "tasks",
    "skills",
    "experience_depth",
    "role_match",
    "industry",
    "special_requirements",
)
VACANCY_SCORE_ALIASES = {
    "experience_depth": "required_years",
    "role_match": "title",
    "special_requirements": "languages",
}
VacancySort = Literal[
    "id",
    "title",
    "state",
    "date",
    "site",
    "total_score",
    "tasks",
    "skills",
    "experience_depth",
    "role_match",
    "industry",
    "special_requirements",
]
VacancySortDirection = Literal["asc", "desc"]
VacancyExportFormat = Literal["csv", "xlsx", "xml"]
VacancyStatusGroup = Literal["SUCCESS", "PROCESSING", "REJECTED", "UNCONFIRMED", "ERROR"]
VACANCY_STATUS_GROUPS = {
    "SUCCESS": frozenset({"SUBMITTED", "ALREADY_APPLIED", "REPORTED"}),
    "PROCESSING": frozenset({"EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING"}),
    "REJECTED": frozenset({"REJECTED_BY_MODEL"}),
    "UNCONFIRMED": frozenset({"UNCONFIRMED"}),
    "ERROR": frozenset({"ERROR"}),
}
VACANCY_EXPORT_HEADERS = (
    "Номер вакансии",
    "Название вакансии",
    "Компания",
    "Сайт",
    "Дата",
    "Общий балл",
    "Задачи",
    "Навыки",
    "Опыт",
    "Роль",
    "Сфера",
    "Особые требования",
)


def _numeric_score(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _evaluation_scores(evaluation: Evaluation | None) -> dict[str, int | float | None]:
    if evaluation is None:
        return {}
    data = evaluation.data or {}
    result: dict[str, int | float | None] = {"total_score": _numeric_score(data.get("score"))}
    breakdown = data.get("score_breakdown")
    if isinstance(breakdown, list):
        for row in breakdown:
            if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                continue
            result[row["key"]] = _numeric_score(row.get("points"))
    for key, alias in VACANCY_SCORE_ALIASES.items():
        if result.get(key) is None and result.get(alias) is not None:
            result[key] = result[alias]
    return result


def _comparable_status_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _public_status_time(vacancy: Vacancy) -> str | None:
    value = vacancy.status_changed_at
    if value is None:
        return None
    # SQLite drops timezone offsets. New rows have a display platform and are
    # written by ``now()`` in UTC; migrated legacy rows intentionally keep the
    # exact local-looking literal requested for 01.09.2026 00:00.
    if vacancy.site and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _export_status_time(vacancy: Vacancy) -> str:
    value = vacancy.status_changed_at
    if value is None:
        return ""
    if vacancy.site:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone()
    return value.strftime("%d.%m.%Y %H:%M")


def _filtered_vacancy_rows(
    db: Session,
    *,
    search: str | None = None,
    state: str | None = None,
    status_group: VacancyStatusGroup | None = None,
    site: str | None = None,
    status_date_from: date | None = None,
    status_date_to: date | None = None,
    total_score_min: float | None = None,
    total_score_max: float | None = None,
    score_limits: dict[str, tuple[float | None, float | None]] | None = None,
    sort: VacancySort = "date",
    sort_dir: VacancySortDirection = "desc",
) -> list[tuple[Vacancy, Evaluation | None]]:
    rows = list(
        db.execute(
            select(Vacancy, Evaluation).outerjoin(
                Evaluation, Evaluation.vacancy_id == Vacancy.id
            )
        ).all()
    )
    needle = (search or "").strip().casefold()
    date_from = datetime.combine(status_date_from, time.min) if status_date_from else None
    date_to = datetime.combine(status_date_to, time.max) if status_date_to else None
    requested_site = "" if site == "__legacy__" else site
    limits = score_limits or {}
    filtered: list[tuple[Vacancy, Evaluation | None]] = []

    state_groups = {
        **VACANCY_STATUS_GROUPS,
        # Keep exact-state filters useful for clients that need one status.
        "SUBMITTED": {"SUBMITTED"},
        "ALREADY_APPLIED": {"ALREADY_APPLIED"},
        "REPORTED": {"REPORTED"},
        "REJECTED_BY_MODEL": {"REJECTED_BY_MODEL"},
        "EXTRACTED": {"EXTRACTED"},
        "EVALUATING": {"EVALUATING"},
        "READY_TO_SUBMIT": {"READY_TO_SUBMIT"},
        "READY_TO_REPORT": {"READY_TO_REPORT"},
        "SUBMITTING": {"SUBMITTING"},
        "UNCONFIRMED": {"UNCONFIRMED"},
    }
    accepted_states: set[str] | None = None
    if status_group:
        accepted_states = set(state_groups[status_group])
    if state:
        requested_states = state_groups.get(state, {state})
        accepted_states = (
            set(requested_states)
            if accepted_states is None
            else accepted_states.intersection(requested_states)
        )

    for vacancy, evaluation in rows:
        haystack = " ".join(
            (
                str(vacancy.id),
                vacancy.external_id or "",
                vacancy.title or "",
                vacancy.company or "",
            )
        ).casefold()
        if needle and needle not in haystack:
            continue
        if accepted_states is not None and vacancy.state not in accepted_states:
            continue
        if requested_site is not None and vacancy.site != requested_site:
            continue
        status_time = _comparable_status_time(vacancy.status_changed_at)
        if date_from and (status_time is None or status_time < date_from):
            continue
        if date_to and (status_time is None or status_time > date_to):
            continue
        scores = _evaluation_scores(evaluation)
        total_score = scores.get("total_score")
        if total_score_min is not None and (
            total_score is None or total_score < total_score_min
        ):
            continue
        if total_score_max is not None and (
            total_score is None or total_score > total_score_max
        ):
            continue
        outside_limit = False
        for key, (minimum, maximum) in limits.items():
            value = scores.get(key)
            if minimum is not None and (value is None or value < minimum):
                outside_limit = True
                break
            if maximum is not None and (value is None or value > maximum):
                outside_limit = True
                break
        if not outside_limit:
            filtered.append((vacancy, evaluation))

    def sort_value(row: tuple[Vacancy, Evaluation | None]) -> object | None:
        vacancy, evaluation = row
        if sort == "id":
            return vacancy.id
        if sort == "title":
            return (vacancy.title or "").casefold()
        if sort == "state":
            return (vacancy.state or "").casefold()
        if sort == "date":
            return _comparable_status_time(vacancy.status_changed_at)
        if sort == "site":
            return (vacancy.site or "").casefold()
        return _evaluation_scores(evaluation).get(sort)

    filtered.sort(key=lambda row: row[0].id)
    populated = [row for row in filtered if sort_value(row) is not None]
    missing = [row for row in filtered if sort_value(row) is None]
    populated.sort(key=sort_value, reverse=sort_dir == "desc")
    return populated + missing


def _vacancy_score_limits(
    *,
    tasks_min: float | None,
    tasks_max: float | None,
    skills_min: float | None,
    skills_max: float | None,
    experience_depth_min: float | None,
    experience_depth_max: float | None,
    role_match_min: float | None,
    role_match_max: float | None,
    industry_min: float | None,
    industry_max: float | None,
    special_requirements_min: float | None,
    special_requirements_max: float | None,
) -> dict[str, tuple[float | None, float | None]]:
    return {
        "tasks": (tasks_min, tasks_max),
        "skills": (skills_min, skills_max),
        "experience_depth": (experience_depth_min, experience_depth_max),
        "role_match": (role_match_min, role_match_max),
        "industry": (industry_min, industry_max),
        "special_requirements": (special_requirements_min, special_requirements_max),
    }


def _public_vacancy(vacancy: Vacancy, evaluation: Evaluation | None, include_data: bool) -> dict:
    status_group = next(
        (group for group, states in VACANCY_STATUS_GROUPS.items() if vacancy.state in states),
        "ERROR",
    )
    result = {
        "id": vacancy.id,
        "session_id": vacancy.session_id,
        "title": vacancy.title,
        "company": vacancy.company,
        "url": vacancy.url,
        "state": vacancy.state,
        "status_group": status_group,
        "source": vacancy.source or "",
        "site": vacancy.site or "",
        "status_changed_at": _public_status_time(vacancy),
        "evaluation": public_evaluation(evaluation.data) if evaluation else None,
    }
    data = vacancy.data or {}
    if vacancy.state in {"ERROR", "UNCONFIRMED"}:
        default_code = "SUBMISSION_UNCONFIRMED" if vacancy.state == "UNCONFIRMED" else "VACANCY_PROCESSING_FAILED"
        default_message = (
            "Площадка не подтвердила результат отправки; отклик мог быть отправлен"
            if vacancy.state == "UNCONFIRMED"
            else "Вакансия не обработана из-за ошибки"
        )
        message = data.get("error_message") or data.get("outcome_message")
        if not isinstance(message, str) or not message.strip() or message.strip() == "[удалено]":
            message = default_message
        result["error_code"] = data.get("error_code") or data.get("outcome_code") or default_code
        result["error_message"] = message
        if vacancy.state == "UNCONFIRMED":
            result["outcome_code"] = result["error_code"]
            result["outcome_message"] = message
    if include_data:
        result["data"] = vacancy.data
    return result


@router.get("/vacancies")
def vacancies(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_data: bool = Query(False),
    search: str | None = Query(None),
    state: str | None = Query(None),
    status_group: VacancyStatusGroup | None = Query(None),
    site: str | None = Query(None),
    status_date_from: date | None = Query(None),
    status_date_to: date | None = Query(None),
    total_score_min: float | None = Query(None, ge=0, le=100),
    total_score_max: float | None = Query(None, ge=0, le=100),
    sort: VacancySort = Query("date"),
    sort_dir: VacancySortDirection = Query("desc"),
    tasks_min: float | None = Query(None, ge=0, le=100),
    tasks_max: float | None = Query(None, ge=0, le=100),
    skills_min: float | None = Query(None, ge=0, le=100),
    skills_max: float | None = Query(None, ge=0, le=100),
    experience_depth_min: float | None = Query(None, ge=0, le=100),
    experience_depth_max: float | None = Query(None, ge=0, le=100),
    role_match_min: float | None = Query(None, ge=0, le=100),
    role_match_max: float | None = Query(None, ge=0, le=100),
    industry_min: float | None = Query(None, ge=0, le=100),
    industry_max: float | None = Query(None, ge=0, le=100),
    special_requirements_min: float | None = Query(None, ge=0, le=100),
    special_requirements_max: float | None = Query(None, ge=0, le=100),
    db: Session = Depends(get_db),
) -> dict:
    all_rows = _filtered_vacancy_rows(
        db,
        search=search,
        state=state,
        status_group=status_group,
        site=site,
        status_date_from=status_date_from,
        status_date_to=status_date_to,
        total_score_min=total_score_min,
        total_score_max=total_score_max,
        score_limits=_vacancy_score_limits(
            tasks_min=tasks_min,
            tasks_max=tasks_max,
            skills_min=skills_min,
            skills_max=skills_max,
            experience_depth_min=experience_depth_min,
            experience_depth_max=experience_depth_max,
            role_match_min=role_match_min,
            role_match_max=role_match_max,
            industry_min=industry_min,
            industry_max=industry_max,
            special_requirements_min=special_requirements_min,
            special_requirements_max=special_requirements_max,
        ),
        sort=sort,
        sort_dir=sort_dir,
    )
    total = len(all_rows)
    page_rows = all_rows[offset : offset + limit]
    rows = [_public_vacancy(vacancy, evaluation, include_data) for vacancy, evaluation in page_rows]
    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


@router.get("/vacancies/export")
def export_vacancies(
    format: VacancyExportFormat = Query("csv"),
    search: str | None = Query(None),
    state: str | None = Query(None),
    status_group: VacancyStatusGroup | None = Query(None),
    site: str | None = Query(None),
    status_date_from: date | None = Query(None),
    status_date_to: date | None = Query(None),
    total_score_min: float | None = Query(None, ge=0, le=100),
    total_score_max: float | None = Query(None, ge=0, le=100),
    sort: VacancySort = Query("date"),
    sort_dir: VacancySortDirection = Query("desc"),
    tasks_min: float | None = Query(None, ge=0, le=100),
    tasks_max: float | None = Query(None, ge=0, le=100),
    skills_min: float | None = Query(None, ge=0, le=100),
    skills_max: float | None = Query(None, ge=0, le=100),
    experience_depth_min: float | None = Query(None, ge=0, le=100),
    experience_depth_max: float | None = Query(None, ge=0, le=100),
    role_match_min: float | None = Query(None, ge=0, le=100),
    role_match_max: float | None = Query(None, ge=0, le=100),
    industry_min: float | None = Query(None, ge=0, le=100),
    industry_max: float | None = Query(None, ge=0, le=100),
    special_requirements_min: float | None = Query(None, ge=0, le=100),
    special_requirements_max: float | None = Query(None, ge=0, le=100),
    db: Session = Depends(get_db),
) -> Response:
    rows = _filtered_vacancy_rows(
        db,
        search=search,
        state=state,
        status_group=status_group,
        site=site,
        status_date_from=status_date_from,
        status_date_to=status_date_to,
        total_score_min=total_score_min,
        total_score_max=total_score_max,
        score_limits=_vacancy_score_limits(
            tasks_min=tasks_min,
            tasks_max=tasks_max,
            skills_min=skills_min,
            skills_max=skills_max,
            experience_depth_min=experience_depth_min,
            experience_depth_max=experience_depth_max,
            role_match_min=role_match_min,
            role_match_max=role_match_max,
            industry_min=industry_min,
            industry_max=industry_max,
            special_requirements_min=special_requirements_min,
            special_requirements_max=special_requirements_max,
        ),
        sort=sort,
        sort_dir=sort_dir,
    )

    def export_values(vacancy: Vacancy, evaluation: Evaluation | None) -> list[object]:
        scores = _evaluation_scores(evaluation)
        return [
            vacancy.id,
            vacancy.title,
            vacancy.company or "",
            vacancy.site or "",
            _export_status_time(vacancy),
            scores.get("total_score") if scores.get("total_score") is not None else "",
            *[
                scores.get(key) if scores.get(key) is not None else ""
                for key in VACANCY_SCORE_KEYS
            ],
        ]

    data = [export_values(vacancy, evaluation) for vacancy, evaluation in rows]
    if format == "xlsx":
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "Вакансии"
        sheet.append(VACANCY_EXPORT_HEADERS)
        for row in data:
            sheet.append(row)
        output = io.BytesIO()
        book.save(output)
        return Response(
            output.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="vacancies.xlsx"'},
        )
    if format == "xml":
        root = ET.Element("vacancies")
        for row in data:
            item = ET.SubElement(root, "vacancy")
            for header, value in zip(VACANCY_EXPORT_HEADERS, row, strict=True):
                field = ET.SubElement(item, "field", name=header)
                field.text = "" if value is None else str(value)
        return Response(
            ET.tostring(root, encoding="utf-8", xml_declaration=True),
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="vacancies.xml"'},
        )
    output = io.StringIO(newline="")
    csv.writer(output).writerows([VACANCY_EXPORT_HEADERS, *data])
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="vacancies.csv"'},
    )


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
