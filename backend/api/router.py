from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.adapters import adapter_registry
from backend.browser.executor import BrowserExecutor
from backend.browser.sessions import get_browser, set_browser
from backend.intelligence.gateway import ModelGateway, ModelUnavailable
from backend.orchestrator.workflow import workflow_manager
from backend.persistence.database import get_db
from backend.persistence.models import (
    BrowserEvent,
    CandidateProfile,
    Evaluation,
    JobSession,
    Report,
    Resume,
    ReviewItem,
    Vacancy,
)
from backend.schemas.domain import (
    RELEVANCE_SCORE_THRESHOLD,
    CandidateProfileData,
    ResumeData,
    SessionStatus,
)
from backend.services.reports import ensure_report_pdf
from backend.services.resume import profile_from_import, resume_from_import, save_and_extract

router = APIRouter(prefix="/api")


class SessionCreate(BaseModel):
    # An outdated client/server pair must fail visibly instead of silently
    # losing launch settings such as unlimited limits.
    model_config = ConfigDict(extra="forbid")
    profile_id: int
    # The relevance cutoff is an application invariant. Keep the field for
    # compatibility with clients that send it, but reject every other value.
    score_threshold: int = Field(
        default=RELEVANCE_SCORE_THRESHOLD,
        ge=RELEVANCE_SCORE_THRESHOLD,
        le=RELEVANCE_SCORE_THRESHOLD,
    )
    adapter_id: str
    mode: str = "analysis_only"
    viewed_limit: int | None = Field(default=30, ge=1)
    application_limit: int | None = Field(default=5, ge=1)


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@router.get("/model/status")
@router.post("/model/check")
async def model_status() -> dict:
    return await ModelGateway().status()


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
        profile.data = _profile_data(profile).model_dump()
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
    item.data = data.model_dump()
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
    return {"id": item.id, "profile_id": item.profile_id, "score_threshold": item.score_threshold, "adapter_id": item.adapter_id, "mode": item.mode, "viewed_limit": item.viewed_limit, "application_limit": item.application_limit, "status": item.status, "counters": item.counters or {}, "started_at": item.started_at.isoformat() if item.started_at else None, "finished_at": item.finished_at.isoformat() if item.finished_at else None, "stop_reason": item.stop_reason}


@router.post("/sessions")
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> dict:
    if not db.get(CandidateProfile, payload.profile_id):
        raise HTTPException(400, "Сначала создайте профиль")
    try: manifest = adapter_registry.get(payload.adapter_id).manifest
    except KeyError as exc: raise HTTPException(400, str(exc)) from exc
    if payload.mode not in manifest.safe_live_modes:
        raise HTTPException(400, f"Режим {payload.mode} не поддерживается адаптером {payload.adapter_id}")
    item = JobSession(profile_id=payload.profile_id, score_threshold=payload.score_threshold, adapter_id=payload.adapter_id, mode=payload.mode, viewed_limit=payload.viewed_limit, application_limit=payload.application_limit, status=SessionStatus.CREATED, counters={})
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
    workflow_manager.launch(session_id)
    return {"ok": True}


@router.post("/sessions/{session_id}/pause")
def pause_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    item.status = SessionStatus.PAUSED; db.commit()
    return session_dict(item)


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    if item.status not in {SessionStatus.PAUSED, SessionStatus.WAITING_FOR_LOGIN}:
        raise HTTPException(409, "Продолжить можно только приостановленную сессию")
    item.status = SessionStatus.RUNNING; item.stop_reason = None; db.commit(); workflow_manager.launch(session_id)
    return session_dict(item)


@router.post("/sessions/{session_id}/stop")
def stop_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    item.status = SessionStatus.STOPPED; item.stop_reason = "Остановлено пользователем"; item.finished_at = datetime.now(timezone.utc); db.commit()
    workflow_manager.ensure_terminal_report(db, item)
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
    executor = BrowserExecutor(adapter.site_id, adapter.allowed_domains, headless=False)
    await executor.start()
    target = "https://hh.ru/"
    set_browser(session_id, executor)
    try:
        await executor.execute("navigate", url=target)
    except Exception:
        return {
            "ok": True,
            "message": (
                "Chromium открыт. Автопереход на HH.ru не завершился; "
                "при необходимости введите https://hh.ru вручную."
            ),
        }
    return {
        "ok": True,
        "message": "Открыт отдельный постоянный профиль Chromium для входа в HH.ru",
    }


@router.post("/sessions/{session_id}/browser/check")
async def check_session_browser(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item:
        raise HTTPException(404, "Сессия не найдена")
    executor = get_browser(session_id)
    if not executor:
        raise HTTPException(400, "Сначала откройте Chromium для HH.ru")
    adapter = adapter_registry.get(item.adapter_id)
    login = await adapter.get_login_state(executor.page)
    if not login.authenticated:
        raise HTTPException(400, login.message)
    item.status = SessionStatus.RUNNING
    item.stop_reason = None
    db.commit()
    workflow_manager.launch(session_id)
    return {"ok": True, "message": "Вход в HH.ru подтверждён, сессия продолжена"}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(JobSession, session_id)
    if not item: raise HTTPException(404, "Сессия не найдена")
    return session_dict(item)


@router.get("/sessions/{session_id}/events")
def events(session_id: int, after: int = 0, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(BrowserEvent).where(BrowserEvent.session_id == session_id, BrowserEvent.id > after).order_by(BrowserEvent.id))
    return [{"id": e.id, "type": e.event_type, "message": e.message, "data": e.data, "created_at": e.created_at.isoformat()} for e in rows]


@router.get("/reviews")
def reviews(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": r.id, "session_id": r.session_id, "vacancy_id": r.vacancy_id, "kind": r.kind, "question": r.question, "status": r.status, "answer": r.answer} for r in db.scalars(select(ReviewItem).order_by(ReviewItem.id.desc()))]


class ReviewAnswer(BaseModel):
    answer: str = ""


def resolve_review(review_id: int, status: str, payload: ReviewAnswer, db: Session) -> dict:
    item = db.get(ReviewItem, review_id)
    if not item: raise HTTPException(404, "Проверка не найдена")
    item.status = status; item.answer = payload.answer; db.commit()
    return {"id": item.id, "status": item.status}


@router.post("/reviews/{review_id}/approve")
def approve_review(review_id: int, payload: ReviewAnswer, db: Session = Depends(get_db)) -> dict:
    return resolve_review(review_id, "approved", payload, db)


@router.post("/reviews/{review_id}/reject")
def reject_review(review_id: int, payload: ReviewAnswer, db: Session = Depends(get_db)) -> dict:
    return resolve_review(review_id, "rejected", payload, db)


@router.get("/vacancies")
def vacancies(db: Session = Depends(get_db)) -> list[dict]:
    rows = []
    for vacancy in db.scalars(select(Vacancy).order_by(Vacancy.id.desc())):
        evaluation = db.scalar(select(Evaluation).where(Evaluation.vacancy_id == vacancy.id))
        rows.append(
            {
                "id": vacancy.id,
                "session_id": vacancy.session_id,
                "title": vacancy.title,
                "company": vacancy.company,
                "url": vacancy.url,
                "state": vacancy.state,
                "data": vacancy.data,
                "evaluation": evaluation.data if evaluation else None,
            }
        )
    return rows


@router.get("/reports")
def reports(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "summary": r.summary,
            "created_at": r.created_at.isoformat(),
            "pdf_url": f"/api/reports/{r.id}/pdf",
        }
        for r in db.scalars(select(Report).order_by(Report.id.desc()))
    ]


@router.get("/reports/{report_id}")
def report(report_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(Report, report_id)
    if not item: raise HTTPException(404, "Отчёт не найден")
    return {"id": item.id, "session_id": item.session_id, "summary": item.summary, "files": {"html": item.html_path, "json": item.json_path, "csv": item.csv_path, "pdf": item.pdf_path}, "pdf_url": f"/api/reports/{item.id}/pdf"}


@router.get("/reports/{report_id}/pdf")
def report_pdf(report_id: int, db: Session = Depends(get_db)) -> FileResponse:
    item = db.get(Report, report_id)
    if not item:
        raise HTTPException(404, "Отчёт не найден")
    try:
        path = ensure_report_pdf(db, item)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise HTTPException(404, "PDF-отчёт не найден") from exc
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"session-{item.session_id}-report.pdf",
    )


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
