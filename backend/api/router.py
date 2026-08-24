from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
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
from backend.intelligence.model_config import validate_base_url
from backend.orchestrator.workflow import workflow_manager
from backend.persistence.crypto import decrypt_secret, encrypt_secret
from backend.persistence.database import get_db
from backend.persistence.models import (
    AIModelSettings,
    BrowserEvent,
    CandidateProfile,
    Evaluation,
    JobSession,
    Notification,
    Resume,
    Vacancy,
)
from backend.schemas.domain import (
    RELEVANCE_SCORE_THRESHOLD,
    CandidateProfileData,
    ResumeData,
    SessionStatus,
)
from backend.services.resume import profile_from_import, resume_from_import, save_and_extract

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
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(
            base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key}
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


@router.post("/model/models")
async def list_model_models(
    payload: ModelModelsIn, request: Request, db: Session = Depends(get_db)
):
    _check_model_origin(request)
    try:
        item = db.get(AIModelSettings, 1)
        base = validate_base_url(
            payload.base_url,
            (settings.openai_base_url, item.base_url if item else ""),
        )
        key = payload.api_key or (
            decrypt_secret(item.encrypted_api_key) if item else ""
        )
        if not key:
            raise ValueError("API ключ не задан")
        return _no_store({"models": await _models(base, key)})
    except Exception as exc:
        raise HTTPException(400, "Не удалось получить список моделей") from exc


@router.put("/model/settings")
async def save_model_settings(
    payload: ModelSettingsIn, request: Request, db: Session = Depends(get_db)
):
    _check_model_origin(request)
    try:
        item = db.get(AIModelSettings, 1)
        base = validate_base_url(
            payload.base_url,
            (settings.openai_base_url, item.base_url if item else ""),
        )
        key = payload.api_key or (decrypt_secret(item.encrypted_api_key) if item else "")
        if not key:
            raise ValueError("API ключ не задан")
        models = await _models(base, key)
        if payload.model not in models:
            raise ValueError("Выбранная модель недоступна")
        if item is None:
            item = AIModelSettings(
                id=1, base_url=base, model=payload.model, encrypted_api_key=encrypt_secret(key)
            )
            db.add(item)
        else:
            item.base_url, item.model = base, payload.model
            if payload.api_key:
                item.encrypted_api_key = encrypt_secret(key)
        db.commit()
        return get_model_settings(db)
    except HTTPException:
        raise
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
def notifications(limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict]:
    items = db.scalars(
        select(Notification).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
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
        update(Notification).where(Notification.read_at.is_(None)).values(read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return {"updated": result.rowcount}


class SessionCreate(BaseModel):
    # An outdated client/server pair must fail visibly instead of silently
    # losing launch settings such as unlimited limits.
    model_config = ConfigDict(extra="forbid")
    profile_id: int
    score_threshold: int = Field(
        default=RELEVANCE_SCORE_THRESHOLD,
        ge=0,
        le=100,
    )
    minimum_scores: dict[str, int] | None = None
    adapter_id: str
    viewed_limit: int | None = Field(default=30, ge=1)
    application_limit: int | None = Field(default=5, ge=1)

    @classmethod
    def _minimum_limits(cls) -> dict[str, int]:
        return {"title": 2, "tasks": 3, "industry": 4, "required_years": 2, "languages": 2, "skills": 3}

    @model_validator(mode="after")
    def validate_minimum_scores(self) -> SessionCreate:
        for key, value in (self.minimum_scores or {}).items():
            maximum = self._minimum_limits().get(key)
            if maximum is None:
                raise ValueError(f"Неизвестный критерий минимального балла: {key}")
            if isinstance(value, bool) or not 0 <= value <= maximum:
                raise ValueError(f"Минимум {key} должен быть от 0 до {maximum}")
        return self


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@router.get("/model/status")
@router.post("/model/check")
async def model_status():
    return _no_store(await ModelGateway().status())


async def _import_resume_for_profile(
    file: UploadFile, profile_id: int | None, db: Session
) -> tuple[CandidateProfile, Resume]:
    if profile_id is not None and not db.get(CandidateProfile, profile_id):
        raise HTTPException(404, "Профиль не найден")
    try:
        path, imported = await save_and_extract(file, ModelGateway())
    except ModelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        profile = db.get(CandidateProfile, profile_id) if profile_id else None
        personal = profile_from_import(imported)
        if profile is None:
            profile = CandidateProfile(
                full_name=personal.full_name,
                residence=personal.residence,
                job_search_locations=personal.job_search_locations,
                contacts=personal.contacts.model_dump(),
                education=[item.model_dump() for item in personal.education],
                languages=[item.model_dump() for item in personal.languages],
                driver_license=personal.driver_license,
            )
            db.add(profile)
            db.flush()
        else:
            _merge_personal_profile(profile, personal)
        resume_data = resume_from_import(imported, path=path, filename=file.filename or "resume")
        resume = Resume(
            profile_id=profile.id,
            **resume_data.model_dump(exclude={"original_filename", "original_path"}),
            original_filename=file.filename or "resume",
            original_path=str(path),
        )
        db.add(resume)
        db.commit()
        db.refresh(profile)
        db.refresh(resume)
    except Exception as exc:
        db.rollback()
        path.unlink(missing_ok=True)
        raise HTTPException(500, "Не удалось сохранить импортированное резюме") from exc
    return profile, resume


@router.post("/profiles/{profile_id}/resumes/import")
async def import_resume_for_profile(
    profile_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)
) -> dict:
    profile, resume = await _import_resume_for_profile(file, profile_id, db)
    return {"profile": serialize_profile(profile, db), "resume": serialize_resume(resume)}


@router.post("/profiles/resume")
async def upload_resume(
    file: UploadFile = File(...), profile_id: int | None = None, db: Session = Depends(get_db)
) -> dict:
    """Legacy alias for imports; canonical clients should use the nested path."""
    profile, _ = await _import_resume_for_profile(file, profile_id, db)
    return serialize_profile(profile, db)


def _profile_data(item: CandidateProfile) -> CandidateProfileData:
    return CandidateProfileData(
        full_name=item.full_name,
        residence=item.residence,
        job_search_locations=item.job_search_locations or [],
        contacts=item.contacts or {},
        education=item.education or [],
        languages=item.languages or [],
        driver_license=item.driver_license,
    )


def _merge_personal_profile(item: CandidateProfile, data: CandidateProfileData) -> None:
    if not item.full_name and data.full_name:
        item.full_name = data.full_name
    if not item.residence and data.residence:
        item.residence = data.residence
    if not item.job_search_locations and data.job_search_locations:
        item.job_search_locations = data.job_search_locations
    if not item.contacts and data.contacts.model_dump(exclude_none=True):
        item.contacts = data.contacts.model_dump()
    if not item.education and data.education:
        item.education = [entry.model_dump() for entry in data.education]
    if not item.languages and data.languages:
        item.languages = [entry.model_dump() for entry in data.languages]
    if item.driver_license is None and data.driver_license is not None:
        item.driver_license = data.driver_license


def _matching_resumes(profile_id: int, db: Session) -> list[dict]:
    return [serialize_resume(item) for item in db.scalars(select(Resume).where(Resume.profile_id == profile_id).order_by(Resume.id))]


def serialize_resume(item: Resume) -> dict:
    return {
        "id": item.id,
        "profile_id": item.profile_id,
        "name": item.name,
        "desired_title": item.desired_title,
        "desired_salary": item.desired_salary,
        "employment_types": item.employment_types or [],
        "work_formats": item.work_formats or [],
        "business_trips": item.business_trips,
        "experiences": item.experiences or [],
        "skills": item.skills or [],
        "about": item.about,
        "selected_for_matching": item.selected_for_matching,
        "original_filename": item.original_filename,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def serialize_profile(item: CandidateProfile, db: Session | None = None) -> dict:
    data = _profile_data(item).model_dump()
    result = {"id": item.id, "data": data, "created_at": item.created_at.isoformat()}
    if db is not None:
        result["resumes"] = _matching_resumes(item.id, db)
    return result


@router.get("/profiles")
def profiles(db: Session = Depends(get_db)) -> list[dict]:
    return [serialize_profile(item, db) for item in db.scalars(select(CandidateProfile).order_by(CandidateProfile.id.desc()))]


@router.post("/profiles")
def create_profile(data: CandidateProfileData, db: Session = Depends(get_db)) -> dict:
    item = CandidateProfile(
        full_name=data.full_name,
        residence=data.residence,
        job_search_locations=data.job_search_locations,
        contacts=data.contacts.model_dump(),
        education=[entry.model_dump() for entry in data.education],
        languages=[entry.model_dump() for entry in data.languages],
        driver_license=data.driver_license,
        data=data.model_dump(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize_profile(item, db)


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(CandidateProfile, profile_id)
    if not item:
        raise HTTPException(404, "Профиль не найден")
    return serialize_profile(item, db)


@router.patch("/profiles/{profile_id}")
def patch_profile(profile_id: int, data: CandidateProfileData, db: Session = Depends(get_db)) -> dict:
    item = db.get(CandidateProfile, profile_id)
    if not item: raise HTTPException(404, "Профиль не найден")
    item.full_name = data.full_name
    item.residence = data.residence
    item.job_search_locations = data.job_search_locations
    item.contacts = data.contacts.model_dump()
    item.education = [entry.model_dump() for entry in data.education]
    item.languages = [entry.model_dump() for entry in data.languages]
    item.driver_license = data.driver_license
    db.commit(); db.refresh(item)
    return serialize_profile(item, db)


def _get_resume_or_404(profile_id: int, resume_id: int, db: Session) -> Resume:
    item = db.get(Resume, resume_id)
    if not item or item.profile_id != profile_id:
        raise HTTPException(404, "Резюме не найдено")
    return item


@router.get("/profiles/{profile_id}/resumes")
def resumes(profile_id: int, db: Session = Depends(get_db)) -> list[dict]:
    if not db.get(CandidateProfile, profile_id):
        raise HTTPException(404, "Профиль не найден")
    return _matching_resumes(profile_id, db)


@router.post("/profiles/{profile_id}/resumes")
def create_resume(profile_id: int, data: ResumeData, db: Session = Depends(get_db)) -> dict:
    if not db.get(CandidateProfile, profile_id):
        raise HTTPException(404, "Профиль не найден")
    item = Resume(profile_id=profile_id, **data.model_dump())
    db.add(item); db.commit(); db.refresh(item)
    return serialize_resume(item)


@router.get("/profiles/{profile_id}/resumes/{resume_id}")
def get_resume(profile_id: int, resume_id: int, db: Session = Depends(get_db)) -> dict:
    return serialize_resume(_get_resume_or_404(profile_id, resume_id, db))


@router.patch("/profiles/{profile_id}/resumes/{resume_id}")
def patch_resume(profile_id: int, resume_id: int, data: ResumeData, db: Session = Depends(get_db)) -> dict:
    item = _get_resume_or_404(profile_id, resume_id, db)
    for key, value in data.model_dump().items():
        setattr(item, key, value)
    db.commit(); db.refresh(item)
    return serialize_resume(item)


@router.delete("/profiles/{profile_id}/resumes/{resume_id}")
def delete_resume(profile_id: int, resume_id: int, db: Session = Depends(get_db)) -> dict:
    item = _get_resume_or_404(profile_id, resume_id, db)
    source_path = Path(item.original_path).resolve() if item.original_path else None
    db.delete(item); db.commit()
    if source_path and source_path.is_file() and Path("data/resumes").resolve() in source_path.parents:
        source_path.unlink(missing_ok=True)
    return {"ok": True}


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
    return {"id": item.id, "profile_id": item.profile_id, "score_threshold": item.score_threshold, "minimum_scores": item.minimum_scores or None, "adapter_id": item.adapter_id, "viewed_limit": item.viewed_limit, "application_limit": item.application_limit, "status": item.status, "counters": item.counters or {}, "started_at": item.started_at.isoformat() if item.started_at else None, "finished_at": item.finished_at.isoformat() if item.finished_at else None, "stop_reason": item.stop_reason}


@router.post("/sessions")
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> dict:
    if not db.get(CandidateProfile, payload.profile_id):
        raise HTTPException(400, "Сначала создайте профиль")
    try: adapter_registry.get(payload.adapter_id)
    except KeyError as exc: raise HTTPException(400, str(exc)) from exc
    item = JobSession(profile_id=payload.profile_id, score_threshold=payload.score_threshold, minimum_scores=payload.minimum_scores or None, adapter_id=payload.adapter_id, viewed_limit=payload.viewed_limit, application_limit=payload.application_limit, status=SessionStatus.CREATED, counters={})
    db.add(item); db.commit(); db.refresh(item)
    return session_dict(item)


@router.get("/sessions")
def sessions(db: Session = Depends(get_db)) -> list[dict]:
    return [session_dict(s) for s in db.scalars(select(JobSession).order_by(JobSession.id.desc()))]


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    if item.status != SessionStatus.CREATED:
        raise HTTPException(409, "Запустить можно только новую сессию")
    if workflow_manager.launch(session_id) is False:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    return {"ok": True}


@router.post("/sessions/{session_id}/pause")
def pause_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(409, "Ручная пауза отключена; сессию можно приостановить только при CAPTCHA")


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    if item.status not in {SessionStatus.PAUSED, SessionStatus.WAITING_FOR_LOGIN}:
        raise HTTPException(409, "Продолжить можно только приостановленную сессию")
    if workflow_manager.launch(session_id) is False:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    item.status = SessionStatus.RUNNING; item.stop_reason = None; db.commit()
    return session_dict(item)


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    item.status = SessionStatus.STOPPED; item.stop_reason = "Остановлено пользователем"; item.finished_at = datetime.now(timezone.utc); db.commit()
    await close_browser(session_id)
    if item.adapter_id == "hirehi":
        workflow_manager.write_hirehi_report(session_id)
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
    target = "https://hirehi.ru/" if item.adapter_id == "hirehi" else "https://hh.ru"
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
        raise HTTPException(400, f"Сначала откройте Chromium для {getattr(adapter_registry.get(item.adapter_id), 'display_name', item.adapter_id)}")
    adapter = adapter_registry.get(item.adapter_id)
    login = await adapter.get_login_state(executor.page)
    if not login.authenticated:
        raise HTTPException(400, login.message)
    if workflow_manager.launch(session_id) is False:
        raise HTTPException(409, "Для этого сайта уже выполняется другая сессия")
    item.status = SessionStatus.RUNNING
    item.stop_reason = None
    db.commit()
    return {"ok": True, "message": f"Вход в {getattr(adapter, 'display_name', item.adapter_id)} подтверждён, сессия продолжена"}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    return session_dict(item)


@router.get("/sessions/{session_id}/report")
def session_report_status(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    if item.adapter_id != "hirehi": raise HTTPException(400, "PDF-отчёт доступен только для HireHi")
    path = Path("output/pdf") / f"hirehi-session-{session_id}.pdf"
    return {"ready": path.is_file(), "pdf_url": f"/api/sessions/{session_id}/report/pdf" if path.is_file() else None}


@router.get("/sessions/{session_id}/report/pdf")
def session_report_pdf(session_id: int, db: Session = Depends(get_db)) -> FileResponse:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    if item.adapter_id != "hirehi": raise HTTPException(400, "PDF-отчёт доступен только для HireHi")
    path = Path("output/pdf") / f"hirehi-session-{session_id}.pdf"
    if not path.is_file(): raise HTTPException(404, "PDF-отчёт ещё не готов")
    return FileResponse(path, media_type="application/pdf", filename=f"hirehi-session-{session_id}.pdf")


@router.get("/sessions/{session_id}/events")
def events(session_id: int, after: int = 0, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(BrowserEvent).where(BrowserEvent.session_id == session_id, BrowserEvent.id > after).order_by(BrowserEvent.id))
    return [{"id": e.id, "type": e.event_type, "message": e.message, "data": e.data, "created_at": e.created_at.isoformat()} for e in rows]


@router.get("/vacancies")
def vacancies(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_data: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict:
    total = db.scalar(select(func.count()).select_from(Vacancy)) or 0
    rows = []
    for vacancy in db.scalars(select(Vacancy).order_by(Vacancy.id.desc()).offset(offset).limit(limit)):
        evaluation = db.scalar(select(Evaluation).where(Evaluation.vacancy_id == vacancy.id))
        rows.append(
            {
                "id": vacancy.id,
                "session_id": vacancy.session_id,
                "title": vacancy.title,
                "company": vacancy.company,
                "url": vacancy.url,
                "state": vacancy.state,
                "evaluation": evaluation.data if evaluation else None,
            }
        )
        if include_data:
            rows[-1]["data"] = vacancy.data
    return {"items": rows, "total": total, "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}


async def session_socket(websocket: WebSocket, session_id: int) -> None:
    await websocket.accept(); after = 0
    try:
        while True:
            from backend.persistence.database import SessionLocal
            with SessionLocal() as db:
                batch = list(db.scalars(select(BrowserEvent).where(BrowserEvent.session_id == session_id, BrowserEvent.id > after).order_by(BrowserEvent.id)))
                for event in batch:
                    await websocket.send_json({"id": event.id, "type": event.event_type, "message": event.message, "data": event.data})
                    after = event.id
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
