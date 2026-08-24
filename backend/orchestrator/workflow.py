from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from backend.adapters import adapter_registry
from backend.browser.sessions import close_browser, get_browser
from backend.intelligence.evaluator import _payload, evaluate
from backend.intelligence.gateway import ModelGateway, ModelUnavailable
from backend.intelligence.hirehi_category import JobSummary, choose_hirehi_category
from backend.intelligence.hirehi_grade import hirehi_grades
from backend.intelligence.letter_writer import write_cover_letter
from backend.orchestrator.application_guard import unresolved_application_questions
from backend.persistence import models as persistence_models
from backend.persistence.database import SessionLocal
from backend.persistence.models import (
    Application,
    ApplicationPlanRecord,
    BrowserEvent,
    CandidateProfile,
    CoverLetter,
    Evaluation,
    JobSession,
    Vacancy,
    VacancySnapshot,
)
from backend.schemas import domain as domain_schemas
from backend.schemas.domain import (
    ApplicationPlan,
    JobEvaluation,
    SessionStatus,
)
from backend.services.hirehi_reporting import write_session_pdf


def _increment_counter(db, item: JobSession, key: str, *, persist: bool = False) -> None:
    counters = dict(item.counters)
    counters[key] = counters.get(key, 0) + 1
    item.counters = counters
    if persist:
        db.commit()


def _limit_reached(count: int, limit: int | None) -> bool:
    """Unlimited session limits are represented by ``None``."""
    return limit is not None and count >= limit


def _application_count(item: JobSession, adapter_id: str) -> int:
    """HireHi collects report entries; other adapters still submit applications."""
    key = "reported" if adapter_id == "hirehi" else "submitted"
    return int((item.counters or {}).get(key, 0))


def _application_limit_reason(adapter_id: str) -> str:
    if adapter_id == "hirehi":
        return "Достигнут лимит выбранных вакансий"
    return "Достигнут лимит отправленных откликов"


def _vacancy_scope(
    adapter_id: str,
    source: str,
    external_id: str | None,
    session_id: int,
) -> list[Any]:
    """HireHi report sessions may safely re-evaluate a posting; submit flows may not."""
    conditions: list[Any] = [
        Vacancy.source == source,
        Vacancy.external_id == external_id,
    ]
    if adapter_id == "hirehi":
        conditions.append(Vacancy.session_id == session_id)
    return conditions


def _blocker_state(kind: str) -> str:
    if kind == "test":
        return "NEEDS_REVIEW"
    if kind == "unknown_form":
        return "UNKNOWN"
    return "ERROR"


def _record_blocker_outcome(db, item: JobSession, blocker: Any, vacancy: Vacancy) -> None:
    vacancy.state = _blocker_state(blocker.kind)
    counters = dict(item.counters)
    if blocker.kind == "test":
        counters["skipped_test"] = counters.get("skipped_test", 0) + 1
        counters["review"] = counters.get("review", 0) + 1
    else:
        counters["errors"] = counters.get("errors", 0) + 1
    item.counters = counters


def _duplicate_event_data(adapter: Any, posting: Any) -> dict[str, Any]:
    """Keep duplicate diagnostics useful without requiring adapter changes."""
    data: dict[str, Any] = {
        "external_id": posting.external_id,
        "source": posting.source,
    }
    page = getattr(adapter, "current_result_page", None)
    if page is not None:
        data["page"] = page
    return data


def _profile_schema():
    return (
        getattr(domain_schemas, "PersonalProfileData", None) or domain_schemas.CandidateProfileData
    )


def _record_payload(record: Any) -> dict:
    """Return a complete ResumeData payload for pydantic, dict, or ORM records."""
    if isinstance(record, dict):
        return _payload(record)
    data = getattr(record, "data", None)
    if isinstance(data, dict):
        payload = _payload(data)
    else:
        payload = {}
        for key in (
            "id",
            "profile_id",
            "name",
            "desired_title",
            "desired_salary",
            "employment_types",
            "work_formats",
            "business_trips",
            "experiences",
            "skills",
            "about",
            "selected_for_matching",
            "original_filename",
            "original_path",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
    for key in ("id", "profile_id", "candidate_profile_id", "selected_for_matching"):
        value = getattr(record, key, None)
        if value is not None:
            payload.setdefault(key, value)
    return payload


def _profile_payload(record: Any) -> dict:
    profile_keys = (
        "full_name",
        "residence",
        "job_search_locations",
        "contacts",
        "education",
        "languages",
        "driver_license",
    )
    payload: dict = {}
    for key in profile_keys:
        value = getattr(record, key, None)
        if value is not None:
            payload[key] = value
    return payload


def _selected_resume_records(db, profile_record: Any) -> list[Any]:
    """Load selected Resume ORM rows, with a JSON fixture fallback for legacy tests."""
    relationship = getattr(profile_record, "resumes", None)
    if relationship is not None:
        return [
            item
            for item in relationship
            if (
                item.get("selected_for_matching")
                if isinstance(item, dict)
                else getattr(item, "selected_for_matching", False)
            )
        ]

    resume_model = getattr(persistence_models, "Resume", None)
    if resume_model is not None:
        profile_column = getattr(resume_model, "profile_id", None)
        if profile_column is None:
            profile_column = getattr(resume_model, "candidate_profile_id", None)
        selected_column = getattr(resume_model, "selected_for_matching", None)
        if profile_column is not None and selected_column is not None:
            statement = select(resume_model).where(
                profile_column == profile_record.id,
                selected_column.is_(True),
            )
            id_column = getattr(resume_model, "id", None)
            if id_column is not None:
                statement = statement.order_by(id_column)
            return list(db.scalars(statement))

    return []


def _resume_desired_title(resume: dict) -> str:
    for key in ("desired_title", "title", "position"):
        value = resume.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("general", "general_info", "common"):
        nested = resume.get(key)
        if isinstance(nested, dict):
            title = _resume_desired_title(nested)
            if title:
                return title
    return ""


_ADJACENT_TITLE_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("product manager", "менеджер продукта", "продакт"),
        ("Product Owner", "Менеджер продукта", "Владелец продукта", "Growth Product Manager"),
    ),
    (
        ("project manager", "менеджер проектов", "проджект"),
        ("Project Manager", "Delivery Manager", "Координатор проектов", "Program Manager"),
    ),
    (
        ("business analyst", "бизнес-аналит"),
        ("Бизнес-аналитик", "Системный аналитик", "IT Analyst", "Product Analyst"),
    ),
    (
        ("system analyst", "системный аналит"),
        ("Системный аналитик", "Бизнес-аналитик", "Integration Analyst", "IT Analyst"),
    ),
    (
        ("product analyst", "продуктовый аналит"),
        ("Продуктовый аналитик", "Data Analyst", "BI Analyst", "Аналитик данных"),
    ),
    (
        ("data analyst", "аналитик данных"),
        ("Data Analyst", "BI Analyst", "Продуктовый аналитик", "Аналитик данных"),
    ),
    (
        ("backend", "бэкенд"),
        ("Backend Developer", "Backend Engineer", "Software Engineer", "Разработчик API"),
    ),
    (
        ("frontend", "фронтенд"),
        ("Frontend Developer", "Frontend Engineer", "Web Developer", "Fullstack Developer"),
    ),
    (
        ("fullstack", "full stack", "фулстек"),
        ("Fullstack Developer", "Software Engineer", "Backend Developer", "Frontend Developer"),
    ),
    (
        ("quality assurance", " qa", "qa ", "тестиров"),
        ("QA Engineer", "Test Engineer", "Инженер по тестированию", "QA Automation"),
    ),
    (
        ("devops", "sre", "platform engineer"),
        ("DevOps Engineer", "SRE", "Platform Engineer", "Cloud Engineer"),
    ),
    (
        ("machine learning", "ml engineer", "data scientist"),
        ("ML Engineer", "Machine Learning Engineer", "Data Scientist", "AI Engineer"),
    ),
    (
        ("data engineer", "инженер данных"),
        ("Data Engineer", "ETL Developer", "Analytics Engineer", "DWH Developer"),
    ),
    (
        ("recruit", "рекрутер", "talent acquisition"),
        ("IT Recruiter", "Talent Acquisition Specialist", "HR Recruiter", "Sourcer"),
    ),
)


def _adjacent_titles(title: str) -> list[str]:
    normalized = f" {title.casefold()} "
    for markers, alternatives in _ADJACENT_TITLE_GROUPS:
        if any(marker in normalized for marker in markers):
            return list(alternatives)
    return []


def build_search_queries(resumes: list[dict], limit: int = 12) -> list[str]:
    """Create bounded, deduplicated broad queries from selected resume data."""
    values: list[str] = []
    for resume in resumes:
        title = _resume_desired_title(resume)
        if title:
            values.append(title)
            values.extend(_adjacent_titles(title))
        skills = resume.get("skills", [])
        if isinstance(skills, list):
            values.extend(str(x).strip() for x in skills if str(x).strip())
        for key in ("adjacent_titles", "related_titles", "keywords"):
            raw = resume.get(key, [])
            if isinstance(raw, str):
                raw = raw.split(",")
            if isinstance(raw, list):
                values.extend(str(x).strip() for x in raw if str(x).strip())
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value[:120])
        if len(result) >= limit:
            break
    return result


def _resume_file(record: Any, payload: dict) -> str:
    for key in (
        "original_path",
        "file_path",
        "original_file",
        "source_file",
    ):
        value = getattr(record, key, None) if not isinstance(record, dict) else record.get(key)
        if value:
            return str(value)
        value = payload.get(key)
        if value:
            return str(value)
    return ""


class WorkflowManager:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.site_leases: dict[str, int] = {}
        self.task_sites: dict[int, str] = {}

    def launch(self, session_id: int) -> bool | None:
        task = self.tasks.get(session_id)
        if task and not task.done():
            return None
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item:
                return False
            site_id = item.adapter_id
            active_statuses = (
                SessionStatus.RUNNING,
                SessionStatus.PAUSED,
                SessionStatus.WAITING_FOR_LOGIN,
                SessionStatus.NEEDS_REVIEW,
            )
            other_active = db.scalar(
                select(JobSession.id).where(
                    JobSession.adapter_id == site_id,
                    JobSession.id != session_id,
                    JobSession.status.in_(active_statuses),
                ).limit(1)
            )
            if other_active is not None:
                return False
        owner = self.site_leases.get(site_id)
        if owner is not None and owner != session_id:
            return False
        self.site_leases[site_id] = session_id
        self.task_sites[session_id] = site_id
        try:
            self.tasks[session_id] = asyncio.create_task(self.run(session_id))
        except Exception:
            self.site_leases.pop(site_id, None)
            self.task_sites.pop(session_id, None)
            raise
        return True

    def emit(
        self, db, session_id: int, event_type: str, message: str, data: dict | None = None
    ) -> None:
        db.add(
            BrowserEvent(
                session_id=session_id, event_type=event_type, message=message, data=data or {}
            )
        )
        db.commit()

    def _write_hirehi_report(self, db, session_id: int) -> int:
        """Write the deterministic report from the session's reported vacancies."""
        item = db.get(JobSession, session_id)
        if not item or item.adapter_id != "hirehi":
            return 0
        vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == session_id)))
        vacancies = [v for v in vacancies if (v.data or {}).get("report_route_kind")]
        rows = []
        for vacancy in vacancies:
            data = vacancy.data or {}
            evaluation = db.scalar(select(Evaluation).where(Evaluation.vacancy_id == vacancy.id))
            rows.append({
                "title": vacancy.title, "company": vacancy.company or "",
                "score": (evaluation.data or {}).get("score") if evaluation else None,
                "hirehi_url": data.get("report_hirehi_url", vacancy.url),
                "route_kind": data.get("report_route_kind", ""),
                "target_url": data.get("report_target_url", ""),
                "contact": data.get("report_contact", ""),
                "short_description": data.get("report_short_description", ""),
                "cover_letter": data.get("report_cover_letter", ""),
            })
        write_session_pdf(session_id, rows)
        self.emit(db, session_id, "report_ready", "PDF отчёт сформирован", {
            "path": f"/api/sessions/{session_id}/report/pdf", "count": len(rows)
        })
        return len(rows)

    def write_hirehi_report(self, session_id: int) -> int:
        """Idempotently generate a HireHi report for completed or stopped sessions."""
        with SessionLocal() as db:
            return self._write_hirehi_report(db, session_id)

    def finalize(self, session_id: int, completion_reason: str) -> None:
        """Finalize only active sessions; preserve externally terminal states."""
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item or item.status == SessionStatus.FAILED:
                return
            was_stopped = item.status == SessionStatus.STOPPED
            if item.adapter_id == "hirehi":
                self._write_hirehi_report(db, session_id)
            if not was_stopped:
                item.status = SessionStatus.COMPLETED
                item.stop_reason = completion_reason
            item.finished_at = datetime.now(timezone.utc)
            db.commit()
            self.emit(db, session_id, "session", "Сессия завершена")

    async def run(self, session_id: int) -> None:
        try:
            await self._run(session_id)
        except Exception as exc:  # task boundary records every unexpected failure
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if item:
                    item.status = SessionStatus.FAILED
                    item.stop_reason = str(exc)
                    item.finished_at = datetime.now(timezone.utc)
                    self.emit(db, session_id, "error", f"Сессия завершилась с ошибкой: {exc}")
        finally:
            with SessionLocal() as db:
                final_item = db.get(JobSession, session_id)
                final_status = final_item.status if final_item else SessionStatus.FAILED
            if final_status in {SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.FAILED}:
                await close_browser(session_id)
            self.tasks.pop(session_id, None)
            site_id = self.task_sites.pop(session_id, None)
            if site_id and self.site_leases.get(site_id) == session_id:
                self.site_leases.pop(site_id, None)

    async def _wait_if_paused(self, session_id: int) -> bool:
        while True:
            with SessionLocal() as db:
                status = db.get(JobSession, session_id).status
            if status == SessionStatus.PAUSED:
                await asyncio.sleep(0.15)
                continue
            return status not in {SessionStatus.STOPPED, SessionStatus.FAILED}

    async def _run(self, session_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item:
                return
            first_start = item.started_at is None
            item.status = SessionStatus.RUNNING
            item.started_at = item.started_at or datetime.now(timezone.utc)
            initial_counters = {
                "viewed": 0,
                "filtered": 0,
                "matched": 0,
                "submitted": 0,
                "reported": 0,
                "already_applied": 0,
                "skipped_test": 0,
                "errors": 0,
            }
            if not first_start:
                initial_counters.update(item.counters or {})
            item.counters = initial_counters
            db.commit()
            self.emit(db, session_id, "session", "Сессия запущена")

        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            profile_record = db.get(CandidateProfile, item.profile_id)
            if profile_record is None:
                raise ValueError(f"Профиль {item.profile_id} не найден")
            profile = _profile_schema().model_validate(_profile_payload(profile_record))
            selected_records = _selected_resume_records(db, profile_record)
            selected_resumes = [_record_payload(resume) for resume in selected_records]
            resume_file = (
                _resume_file(selected_records[0], selected_resumes[0]) if selected_records else ""
            )
            minimum_scores = item.minimum_scores or None
            adapter_id = item.adapter_id

        adapter = adapter_registry.get(adapter_id)
        executor = get_browser(session_id)
        if not executor:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.WAITING_FOR_LOGIN
                item.stop_reason = f"Откройте отдельный Chromium и войдите в {getattr(adapter, 'display_name', adapter_id)} вручную"
                db.commit()
                self.emit(db, session_id, "human_required", item.stop_reason)
            return

        login = await adapter.get_login_state(executor.page)
        if not login.authenticated:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.WAITING_FOR_LOGIN
                item.stop_reason = login.message
                db.commit()
                self.emit(db, session_id, "human_required", login.message)
            return

        if not selected_records:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.FAILED
                item.stop_reason = "Для оценки вакансий не выбрано ни одного резюме"
                item.finished_at = datetime.now(timezone.utc)
                db.commit()
                self.emit(db, session_id, "profile_error", item.stop_reason)
            return

        gateway = ModelGateway()
        hirehi_category: str | None = None
        hirehi_grade_values: list[str] | None = None
        search_filters = {
            "query": _resume_desired_title(selected_resumes[0]),
            "queries": build_search_queries(selected_resumes),
        }
        if adapter_id == "hirehi":
            choice = await choose_hirehi_category(gateway, selected_resumes[0])
            hirehi_category = choice.category
            experience_years, hirehi_grade_values = hirehi_grades(selected_resumes[0])
            search_filters = {"category": choice.category, "grades": hirehi_grade_values}
            with SessionLocal() as event_db:
                self.emit(event_db, session_id, "hirehi_category_selected", choice.reason, {"category": choice.category})
                self.emit(
                    event_db, session_id, "hirehi_grades_selected",
                    "Грейды HireHi выбраны по опыту резюме",
                    {"years": experience_years, "grades": hirehi_grade_values},
                )
                event_db.commit()
        await adapter.open_search(executor.page, search_filters)
        blockers = await adapter.detect_blockers(executor.page)
        if blockers:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                blocker = blockers[0]
                if blocker.kind == "captcha":
                    item.status = SessionStatus.PAUSED
                    item.stop_reason = blocker.message
                    db.commit()
                    self.emit(
                        db, session_id, "human_required", blocker.message, {"kind": blocker.kind}
                    )
                    return
                self.emit(
                    db, session_id, "blocker_skipped", blocker.message, {"kind": blocker.kind}
                )
        refs = await adapter.collect_job_refs(executor.page)
        collect_more = getattr(adapter, "collect_more_job_refs", None)
        seen_ref_ids = {ref.external_id for ref in refs}
        if not refs and collect_more is not None:
            refs.extend(await collect_more(executor.page))
            seen_ref_ids.update(ref.external_id for ref in refs)
        if adapter_id == "hirehi":
            with SessionLocal() as event_db:
                self.emit(
                    event_db,
                    session_id,
                    "hirehi_search_results",
                    "HireHi выдача собрана",
                    {"category": hirehi_category, "count": len(refs)},
                )
                event_db.commit()
        if not refs:
            page_text = (await executor.page.locator("body").inner_text())[:1_000]
            with SessionLocal() as db:
                self.emit(
                    db,
                    session_id,
                    "search_empty",
                    "HH.ru не вернул ссылки на вакансии",
                    {"url": executor.page.url, "page_text": page_text},
                )

        completion_reason = "Доступная выдача обработана"

        async def refill_if_exhausted() -> bool:
            """Refill only after rechecking state and session limits."""
            nonlocal collect_more, completion_reason
            if collect_more is None:
                return False
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if not item:
                    collect_more = None
                    return False
                if item.status in {SessionStatus.STOPPED, SessionStatus.FAILED}:
                    collect_more = None
                    return False
                if _limit_reached(
                    item.counters.get("viewed", 0), item.viewed_limit
                ) or _limit_reached(_application_count(item, adapter_id), item.application_limit):
                    if _limit_reached(item.counters.get("viewed", 0), item.viewed_limit):
                        completion_reason = "Достигнут лимит просмотра вакансий"
                    else:
                        completion_reason = _application_limit_reason(adapter_id)
                    collect_more = None
                    return False
            next_refs = await collect_more(executor.page)
            while not next_refs and not getattr(adapter, "search_exhausted", True):
                next_refs = await collect_more(executor.page)
            new_refs = [
                candidate for candidate in next_refs if candidate.external_id not in seen_ref_ids
            ]
            if new_refs:
                refs.extend(new_refs)
                seen_ref_ids.update(ref.external_id for ref in new_refs)
                return True
            elif getattr(adapter, "search_exhausted", True):
                collect_more = None
            return False

        ref_index = 0
        while ref_index < len(refs) or collect_more is not None:
            if ref_index >= len(refs):
                if not await refill_if_exhausted() and collect_more is None:
                    break
                continue
            ref = refs[ref_index]
            ref_index += 1
            if not await self._wait_if_paused(session_id):
                break
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                viewed_limit = item.viewed_limit
                application_limit = item.application_limit
                if _limit_reached(item.counters.get("viewed", 0), viewed_limit):
                    completion_reason = "Достигнут лимит просмотра вакансий"
                    break
                if _limit_reached(_application_count(item, adapter_id), application_limit):
                    completion_reason = _application_limit_reason(adapter_id)
                    break
            try:
                await adapter.open_job(executor.page, ref)
                blockers = await adapter.detect_blockers(executor.page)
                if blockers:
                    with SessionLocal() as db:
                        item = db.get(JobSession, session_id)
                        blocker = blockers[0]
                        if blocker.kind == "captcha":
                            item.status = SessionStatus.PAUSED
                            item.stop_reason = blocker.message
                        else:
                            vacancy = db.scalar(
                                select(Vacancy).where(
                                    *_vacancy_scope(
                                        adapter_id,
                                        adapter.site_id,
                                        ref.external_id,
                                        session_id,
                                    )
                                )
                            )
                            if vacancy is None:
                                vacancy = Vacancy(
                                    session_id=session_id,
                                    source=adapter.site_id,
                                    external_id=ref.external_id,
                                    url=ref.url,
                                    title=ref.external_id,
                                    state=_blocker_state(blocker.kind),
                                    data={"blocker": blocker.kind, "message": blocker.message},
                                )
                                db.add(vacancy)
                                db.flush()
                            _record_blocker_outcome(db, item, blocker, vacancy)
                            self.emit(
                                db,
                                session_id,
                                "blocker_skipped",
                                blocker.message,
                                {"kind": blocker.kind, "external_id": ref.external_id},
                            )
                        db.commit()
                        if blocker.kind == "captcha":
                            self.emit(db, session_id, "human_required", blocker.message)
                            return
                    continue
                posting = await adapter.extract_job(executor.page)
            except Exception as exc:
                with SessionLocal() as db:
                    item = db.get(JobSession, session_id)
                    vacancy = db.scalar(
                        select(Vacancy).where(
                            *_vacancy_scope(
                                adapter_id,
                                adapter.site_id,
                                ref.external_id,
                                session_id,
                            )
                        )
                    )
                    if vacancy is None:
                        vacancy = Vacancy(
                            session_id=session_id,
                            source=adapter.site_id,
                            external_id=ref.external_id,
                            url=ref.url,
                            title=ref.external_id,
                            state="ERROR",
                            data={"error": str(exc)},
                        )
                        db.add(vacancy)
                    counters = dict(item.counters)
                    counters["errors"] = counters.get("errors", 0) + 1
                    item.counters = counters
                    self.emit(
                        db, session_id, "browser_error", f"Не удалось прочитать вакансию: {exc}"
                    )
                continue
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                existing = db.scalar(
                    select(Vacancy).where(
                        *_vacancy_scope(
                            adapter_id,
                            posting.source,
                            posting.external_id,
                            session_id,
                        )
                    )
                )
                if existing and existing.session_id != session_id:
                    self.emit(
                        db,
                        session_id,
                        "duplicate",
                        f"Дубликат пропущен: {posting.title} ({posting.source}:{posting.external_id})",
                        _duplicate_event_data(adapter, posting),
                    )
                    continue
                if existing and existing.state not in {
                    "EXTRACTED",
                    "EVALUATING",
                    "READY_TO_SUBMIT",
                    "READY_TO_REPORT",
                }:
                    self.emit(
                        db,
                        session_id,
                        "duplicate",
                        f"Вакансия уже обработана: {posting.title} ({posting.source}:{posting.external_id})",
                        _duplicate_event_data(adapter, posting),
                    )
                    continue
                vacancy = existing
                if vacancy is None:
                    vacancy = Vacancy(
                        session_id=session_id,
                        source=posting.source,
                        external_id=posting.external_id,
                        url=posting.url,
                        title=posting.title,
                        company=posting.company,
                        state="EXTRACTED",
                        data=posting.model_dump(mode="json"),
                    )
                    db.add(vacancy)
                    db.commit()
                    db.refresh(vacancy)
                    db.add(VacancySnapshot(vacancy_id=vacancy.id, content=posting.description))
                    counters = dict(item.counters)
                    counters["viewed"] += 1
                    item.counters = counters
                    db.commit()
                    self.emit(
                        db,
                        session_id,
                        "vacancy",
                        f"Извлечена вакансия: {posting.title}",
                        {"vacancy_id": vacancy.id},
                    )

                vacancy.state = "EVALUATING"
                db.commit()
                evaluation_record = db.scalar(
                    select(Evaluation).where(Evaluation.vacancy_id == vacancy.id)
                )
                if evaluation_record:
                    result = JobEvaluation.model_validate(evaluation_record.data)
                else:
                    try:
                        # Keep an auditable, PII-free copy of exactly the job object
                        # supplied to the evaluator (profile/resume stay out of it).
                        self.emit(
                            db,
                            session_id,
                            "evaluation_payload",
                            "Payload вакансии передан на оценку",
                            {"vacancy_id": vacancy.id, "job": _payload(posting),
                             "criteria": ["title", "tasks", "industry", "required_years", "languages", "skills"],
                             "minimum_scores": minimum_scores},
                        )
                        result = await evaluate(
                            posting,
                            profile,
                            selected_resumes,
                            gateway,
                            minimum_scores,
                        )
                    except ModelUnavailable as exc:
                        vacancy.state = "ERROR"
                        counters = dict(item.counters)
                        counters["errors"] = counters.get("errors", 0) + 1
                        item.counters = counters
                        db.commit()
                        self.emit(db, session_id, "model_unavailable", str(exc))
                        continue
                db.refresh(item)
                if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                    return
                if evaluation_record is None:
                    db.add(Evaluation(vacancy_id=vacancy.id, data=result.model_dump()))
                    self.emit(
                        db,
                        session_id,
                        "evaluation",
                        f"Оценка {result.score}/100",
                        {
                            "vacancy_id": vacancy.id,
                            "score": result.score,
                            "decision": result.decision,
                            "minimum_score_violations": result.minimum_score_violations,
                            "breakdown": [row.model_dump() for row in result.score_breakdown],
                        },
                    )
                if result.decision == "skip":
                    vacancy.state = "REJECTED_BY_MODEL"
                    counters = dict(item.counters)
                    counters["filtered"] += 1
                    item.counters = counters
                elif result.decision == "manual_review":
                    vacancy.state = "ERROR"
                    counters = dict(item.counters)
                    counters["errors"] = counters.get("errors", 0) + 1
                    item.counters = counters
                    self.emit(
                        db,
                        session_id,
                        "evaluation_skipped",
                        result.reason,
                        {"vacancy_id": vacancy.id},
                    )
                else:
                    if evaluation_record is None:
                        _increment_counter(db, item, "matched", persist=True)
                    # Persist before awaiting the model. A later refresh would
                    # otherwise discard the dirty JSON counter value.
                    plan = ApplicationPlan(
                        vacancy_id=vacancy.id,
                        resume_file=resume_file,
                        unknown_question_policy="manual_review",
                        submission_allowed=adapter_id != "hirehi",
                    )
                    plan_record = db.scalar(
                        select(ApplicationPlanRecord).where(
                            ApplicationPlanRecord.vacancy_id == vacancy.id
                        )
                    )
                    if plan_record:
                        plan = ApplicationPlan.model_validate(plan_record.data)
                    else:
                        plan_record = ApplicationPlanRecord(
                            vacancy_id=vacancy.id, data=plan.model_dump()
                        )
                        db.add(plan_record)
                    cover_record = db.scalar(
                        select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id)
                    )
                    if cover_record:
                        letter = cover_record.text
                    else:
                        try:
                            letter = await write_cover_letter(
                                posting, profile, selected_resumes, gateway
                            )
                        except ModelUnavailable as exc:
                            vacancy.state = "ERROR"
                            counters = dict(item.counters)
                            counters["errors"] = counters.get("errors", 0) + 1
                            item.counters = counters
                            db.commit()
                            self.emit(db, session_id, "model_unavailable", str(exc))
                            continue
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                        return
                    plan.cover_letter = letter
                    plan_record.data = plan.model_dump()
                    if cover_record is None:
                        db.add(CoverLetter(vacancy_id=vacancy.id, text=letter))
                    vacancy.state = "READY_TO_REPORT" if adapter_id == "hirehi" else "READY_TO_SUBMIT"
                if vacancy.state in {"READY_TO_SUBMIT", "READY_TO_REPORT"}:
                    db.commit()
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                        return
                    blockers = await adapter.detect_blockers(executor.page)
                    if blockers:
                        blocker = blockers[0]
                        if blocker.kind == "captcha":
                            item.status = SessionStatus.PAUSED
                            item.stop_reason = blocker.message
                            self.emit(db, session_id, "human_required", blocker.message)
                            db.commit()
                            return
                        _record_blocker_outcome(db, item, blocker, vacancy)
                        self.emit(
                            db,
                            session_id,
                            "blocker_skipped",
                            blocker.message,
                            {"kind": blocker.kind, "vacancy_id": vacancy.id},
                        )
                        db.commit()
                        continue
                    try:
                        if adapter_id == "hirehi":
                            route_reader = getattr(adapter, "collect_application_route", None)
                            route = await route_reader(executor.page) if route_reader else None
                            form = None
                        else:
                            form = await adapter.open_application(executor.page)
                            route = getattr(form, "route", None)
                        kind = getattr(route, "kind", None)
                        if adapter_id == "hirehi":
                            contact = getattr(route, "contact", None) or getattr(form, "employer_contact", None)
                            contact_text = ", ".join(
                                str(getattr(contact, key))
                                for key in ("email", "telegram", "linkedin")
                                if contact and getattr(contact, key, None)
                            )
                            if not contact_text and contact and getattr(contact, "exhausted", False):
                                contact_text = "Лимит прямых контактов HireHi исчерпан"
                            try:
                                summary = await gateway.structured("job_summary", {"job": {"title": vacancy.title, "description": getattr(posting, "description", "")}}, JobSummary)
                                short_description = summary.summary
                            except Exception:
                                short_description = vacancy.title
                            target_url = ""
                            if kind == "external_employer":
                                target_url = getattr(route, "target_url", None) or ""
                            vacancy.data = {**(vacancy.data or {}), "report_route_kind": kind or "unknown", "report_target_url": target_url, "report_hirehi_url": vacancy.url, "report_contact": contact_text, "report_short_description": short_description, "report_cover_letter": plan.cover_letter or ""}
                            vacancy.state = "REPORTED"
                            counters = dict(item.counters); counters["reported"] = counters.get("reported", 0) + 1; item.counters = counters
                            self.emit(db, session_id, "vacancy_reported", "Вакансия добавлена в отчёт", {"vacancy_id": vacancy.id, "route_kind": kind or "unknown"})
                            db.commit()
                            # HireHi is a report-only pipeline. Route collection above
                            # may reveal contact data, but no application API is allowed.
                            continue
                        if kind == "unknown":
                            vacancy.state = "UNKNOWN"
                            self.emit(
                                db,
                                session_id,
                                "unknown_form",
                                "Неизвестный маршрут отклика",
                                {"vacancy_id": vacancy.id},
                            )
                            db.commit()
                            continue
                        result = await adapter.fill_application(executor.page, plan)
                    except Exception as exc:
                        vacancy.state = "UNKNOWN"
                        counters = dict(item.counters)
                        counters["errors"] = counters.get("errors", 0) + 1
                        item.counters = counters
                        self.emit(
                            db,
                            session_id,
                            "unknown_form",
                            f"Форма отклика недоступна: {exc}",
                            {"vacancy_id": vacancy.id},
                        )
                        db.commit()
                        continue
                    questions = unresolved_application_questions(form, result)
                    if questions:
                        vacancy.state = "UNKNOWN"
                        counters = dict(item.counters)
                        counters["errors"] = counters.get("errors", 0) + 1
                        item.counters = counters
                        self.emit(
                            db,
                            session_id,
                            "unknown_form",
                            "; ".join(questions),
                            {"vacancy_id": vacancy.id},
                        )
                        db.commit()
                        continue
                    else:
                        db.refresh(item)
                        if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                            return
                        try:
                            submission = await adapter.submit_application(executor.page)
                        except Exception as exc:
                            vacancy.state = "ERROR"
                            counters = dict(item.counters)
                            counters["errors"] = counters.get("errors", 0) + 1
                            item.counters = counters
                            self.emit(
                                db,
                                session_id,
                                "submission_error",
                                f"Не удалось отправить отклик: {exc}",
                                {"vacancy_id": vacancy.id},
                            )
                            db.commit()
                            continue
                        vacancy.state = submission.status.upper()
                        db.add(
                            Application(
                                candidate_profile_id=item.profile_id,
                                vacancy_id=vacancy.id,
                                status=submission.status,
                                submitted_at=datetime.now(timezone.utc)
                                if submission.status == "submitted"
                                else None,
                            )
                        )
                        if submission.status == "submitted":
                            counters = dict(item.counters)
                            counters["submitted"] += 1
                            item.counters = counters
                        elif submission.status == "already_applied":
                            counters = dict(item.counters)
                            counters["already_applied"] = counters.get("already_applied", 0) + 1
                            item.counters = counters
                        elif submission.status == "unknown":
                            counters = dict(item.counters)
                            counters["errors"] += 1
                            item.counters = counters
                        self.emit(
                            db,
                            session_id,
                            "submission",
                            submission.message,
                            {"vacancy_id": vacancy.id, "status": submission.status},
                        )
                db.commit()
            await asyncio.sleep(0.05)

        self.finalize(session_id, completion_reason)


workflow_manager = WorkflowManager()


def recover_orphaned_sessions() -> list[int]:
    """Fail process-owned sessions left active by a previous backend process."""
    recovered: list[int] = []
    with SessionLocal() as db:
        orphaned = list(
            db.scalars(
                select(JobSession).where(
                    JobSession.status.in_((SessionStatus.CREATED, SessionStatus.RUNNING))
                )
            )
        )
        for item in orphaned:
            item.status = SessionStatus.FAILED
            item.stop_reason = "Сессия прервана перезапуском платформы; запустите новую сессию"
            item.finished_at = datetime.now(timezone.utc)
            workflow_manager.emit(
                db,
                item.id,
                "recovery",
                item.stop_reason,
            )
            recovered.append(item.id)
    return recovered
