from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from backend.adapters import adapter_registry
from backend.adapters.base.protocol import JobRef
from backend.browser.sessions import close_browser, get_browser, restore_browser
from backend.intelligence.evaluator import _payload, evaluate
from backend.intelligence.gateway import ModelGateway, ModelUnavailable
from backend.intelligence.hirehi_category import JobSummary, choose_hirehi_category
from backend.intelligence.hirehi_grade import hirehi_grades
from backend.intelligence.letter_writer import write_cover_letter
from backend.intelligence.preference_policy import compile_preference_policy
from backend.intelligence.search_planner import plan_search_queries
from backend.orchestrator.application_guard import unresolved_application_questions
from backend.orchestrator.recovery import (
    AuthenticationPending,
    CaptchaRequired,
    RecoverableFailure,
    RecoveryAdapter,
)
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
    DesiredJobPolicy,
    JobEvaluation,
    SessionStatus,
)
from backend.services import search_metrics
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
    query = getattr(adapter, "current_search_query", None)
    if query is not None:
        data["query"] = query
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
    retry_base_seconds = 5
    retry_max_seconds = 300

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
        search_metrics.flush(db, session_id)
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
            item.recovery = {**(item.recovery or {}), "pending_refs": [], "retry_at": None, "message": None}
            item.finished_at = datetime.now(timezone.utc)
            db.commit()
            self.emit(db, session_id, "session", "Сессия завершена")

    async def run(self, session_id: int) -> None:
        metric_token = search_metrics.begin()
        try:
            while True:
                try:
                    await self._run(session_id)
                    break
                except CaptchaRequired as exc:
                    with SessionLocal() as db:
                        item = db.get(JobSession, session_id)
                        if item and item.status not in {SessionStatus.STOPPED, SessionStatus.COMPLETED}:
                            item.status = SessionStatus.PAUSED
                            item.stop_reason = str(exc)
                            self.emit(db, session_id, "human_required", str(exc), {"kind": "captcha"})
                    break
                except Exception as exc:
                    with search_metrics.measure("recovery"):
                        recovered = await self._recover(session_id, exc)
                    if not recovered:
                        break
        finally:
            with SessionLocal() as db:
                if search_metrics.flush(db, session_id):
                    db.commit()
                final_item = db.get(JobSession, session_id)
                final_status = final_item.status if final_item else SessionStatus.FAILED
                if final_item and final_status in {SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.FAILED}:
                    search_metrics.freeze(db, final_item)
            search_metrics.end(metric_token)
            if final_status in {SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.FAILED}:
                with suppress(Exception):
                    await asyncio.wait_for(close_browser(session_id), timeout=15)
            self.tasks.pop(session_id, None)
            site_id = self.task_sites.pop(session_id, None)
            if site_id and self.site_leases.get(site_id) == session_id:
                self.site_leases.pop(site_id, None)

    async def _recover(self, session_id: int, exc: Exception) -> bool:
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item or item.status in {SessionStatus.STOPPED, SessionStatus.COMPLETED, SessionStatus.PAUSED}:
                return False
            recovery = dict(item.recovery or {})
            attempt = recovery.get("attempt", 0) + 1
            delay = min(self.retry_max_seconds, self.retry_base_seconds * 2 ** min(attempt - 1, 10))
            if isinstance(exc, AuthenticationPending):
                delay = self.retry_base_seconds
            if isinstance(exc, AuthenticationPending):
                reason = "Ожидаем вход в открытом браузере; проверка продолжится автоматически"
            elif isinstance(exc, ModelUnavailable):
                reason = "Модель временно недоступна"
            else:
                reason = "Временный сбой обработки или загрузки страницы"
            message = f"{reason}. Автоматический повтор через {delay} с."
            recovery.update(attempt=attempt, retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(), message=message)
            item.recovery = recovery
            item.status = SessionStatus.RUNNING
            item.stop_reason = message
            item.finished_at = None
            self.emit(db, session_id, "recovery_retry", message, {"attempt": attempt, "delay_seconds": delay, "error_type": type(exc).__name__})
        # Short waits keep a user's Stop responsive and never relinquish the site lease.
        deadline = asyncio.get_running_loop().time() + delay
        while True:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if not item or item.status != SessionStatus.RUNNING:
                    return False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 0.25))
        if not isinstance(exc, (ModelUnavailable, AuthenticationPending)):
            with suppress(Exception):
                await asyncio.wait_for(close_browser(session_id), timeout=15)
        return True

    async def _wait_if_paused(self, session_id: int) -> bool:
        while True:
            with SessionLocal() as db:
                status = db.get(JobSession, session_id).status
            if status == SessionStatus.PAUSED:
                await asyncio.sleep(0.15)
                continue
            return status not in {SessionStatus.STOPPED, SessionStatus.FAILED}

    def _save_refs(self, session_id: int, refs: list[JobRef], adapter=None) -> list[JobRef]:
        """Persist discovery before extraction so vanished listings cannot lose work."""
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            recovery = dict(item.recovery or {})
            queued = {ref["external_id"]: ref for ref in recovery.get("pending_refs", [])}
            queued.update({ref.external_id: ref.model_dump() for ref in refs})
            # Include unfinished work from sessions started before queue persistence.
            uncertain = []
            for vacancy in db.scalars(select(Vacancy).where(
                Vacancy.session_id == session_id,
                Vacancy.state.in_(("EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING")),
            )):
                queued.setdefault(vacancy.external_id, {"external_id": vacancy.external_id, "url": vacancy.url})
                if vacancy.state == "SUBMITTING":
                    uncertain.append(vacancy.external_id)
            done = set(db.scalars(select(Vacancy.external_id).where(
                Vacancy.session_id == session_id,
                Vacancy.state.not_in(("EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING")),
            )))
            # Resolve possible sends before consuming the remaining application budget.
            uncertain_ids = set(uncertain)
            ordered_keys = [*uncertain, *(key for key in queued if key not in uncertain_ids)]
            recovery["pending_refs"] = [queued[key] for key in ordered_keys if key not in done]
            checkpoint = getattr(adapter, "search_checkpoint", None)
            if checkpoint:
                recovery["search_checkpoint"] = checkpoint()
            item.recovery = recovery
            search_metrics.flush(db, session_id)
            db.commit()
            return [JobRef.model_validate(ref) for ref in recovery["pending_refs"]]

    def _record_submission(self, db, item, vacancy, submission) -> None:
        vacancy.state = submission.status.upper()
        existing = db.scalar(select(Application).where(Application.vacancy_id == vacancy.id))
        if existing is None:
            db.add(Application(candidate_profile_id=item.profile_id, vacancy_id=vacancy.id,
                               status=submission.status,
                               submitted_at=datetime.now(timezone.utc) if submission.status == "submitted" else None))
            key = submission.status if submission.status in {"submitted", "already_applied"} else "errors"
            _increment_counter(db, item, key)
        # The outcome and its counter are one transaction, including crash recovery.
        self.emit(db, item.id, "submission", submission.message,
                  {"vacancy_id": vacancy.id, "status": submission.status})

    async def _run(self, session_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item:
                return
            if item.status in {SessionStatus.STOPPED, SessionStatus.COMPLETED}:
                return
            first_start = item.started_at is None
            item.status = SessionStatus.RUNNING
            item.started_at = item.started_at or datetime.now(timezone.utc)
            item.stop_reason = None
            item.finished_at = None
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
            if _limit_reached(_application_count(item, item.adapter_id), item.application_limit):
                self.finalize(session_id, _application_limit_reason(item.adapter_id))
                return

        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            profile_record = db.get(CandidateProfile, item.profile_id)
            if profile_record is None:
                raise ValueError(f"Профиль {item.profile_id} не найден")
            profile = _profile_schema().model_validate(_profile_payload(profile_record))
            selected_records = _selected_resume_records(db, profile_record)
            selected_resumes = [_record_payload(resume) for resume in selected_records]
            search_metrics.initialize(db, item, _profile_payload(profile_record), selected_resumes)
            resume_file = (
                _resume_file(selected_records[0], selected_resumes[0]) if selected_records else ""
            )
            minimum_scores = item.minimum_scores or None
            stored_policy = item.preference_policy
            preference_description = getattr(item, "desired_job_description", "") or ""
            preference_policy = (
                DesiredJobPolicy.model_validate(stored_policy)
                if preference_description and stored_policy
                else None
            )
            adapter_id = item.adapter_id

        adapter = RecoveryAdapter(adapter_registry.get(adapter_id))
        executor = get_browser(session_id)
        if not executor:
            executor = await restore_browser(session_id, adapter)

        login = await adapter.get_login_state(executor.page)
        if not login.authenticated:
            # Keep the login page open; automatically notice a restored login.
            await adapter._captcha(executor.page)
            raise AuthenticationPending("Ожидание восстановления авторизации на сайте")

        if not selected_records:
            raise RecoverableFailure("Для оценки вакансий не выбрано ни одного резюме")

        gateway = ModelGateway()
        if preference_description and stored_policy is None:
            preference_policy = await compile_preference_policy(gateway, preference_description)
            with SessionLocal() as db:
                db.get(JobSession, session_id).preference_policy = preference_policy.model_dump(mode="json")
                db.commit()
        hirehi_category: str | None = None
        hirehi_grade_values: list[str] | None = None
        with SessionLocal() as db:
            search_filters = (db.get(JobSession, session_id).recovery or {}).get("search_filters")
            saved_cursor = (db.get(JobSession, session_id).recovery or {}).get("search_checkpoint")
        if search_filters is not None:
            hirehi_category = search_filters.get("category")
        elif adapter_id == "hirehi":
            choice = await choose_hirehi_category(gateway, selected_resumes[0], preference_policy)
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
        else:
            planned_queries = await plan_search_queries(gateway, selected_resumes, preference_policy=preference_policy)
            search_filters = {"queries": planned_queries}
            with SessionLocal() as event_db:
                self.emit(event_db, session_id, "search_plan", "Сформирован план поисковых запросов", {
                    "desired_title": _resume_desired_title(selected_resumes[0]), "queries": planned_queries,
                })
                event_db.commit()
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            item.recovery = {**(item.recovery or {}), "search_filters": search_filters}
            db.commit()
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
        restore_cursor = getattr(adapter, "restore_search_checkpoint", None)
        if saved_cursor is not None and restore_cursor:
            restore_cursor(saved_cursor)
            refs = []
        else:
            refs = await adapter.collect_job_refs(executor.page)
        collect_more = getattr(adapter, "collect_more_job_refs", None)
        seen_ref_ids = {ref.external_id for ref in refs}
        if not refs and collect_more is not None and saved_cursor is None:
            refs.extend(await collect_more(executor.page))
            seen_ref_ids.update(ref.external_id for ref in refs)
        refs = self._save_refs(session_id, refs, adapter)
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
                    f"{getattr(adapter, 'display_name', adapter_id)} не вернул ссылки на вакансии",
                    {"url": executor.page.url, "page_text": page_text},
                )

        completion_reason = "Доступная выдача обработана"
        retry_needed = False

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
                if _limit_reached(_application_count(item, adapter_id), item.application_limit):
                    completion_reason = _application_limit_reason(adapter_id)
                    collect_more = None
                    return False
            next_refs = await collect_more(executor.page)
            self._save_refs(session_id, next_refs, adapter)
            while not next_refs and not getattr(adapter, "search_exhausted", True):
                if not await self._wait_if_paused(session_id):
                    collect_more = None
                    return False
                next_refs = await collect_more(executor.page)
                self._save_refs(session_id, next_refs, adapter)
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
                application_limit = item.application_limit
                if _limit_reached(_application_count(item, adapter_id), application_limit):
                    completion_reason = _application_limit_reason(adapter_id)
                    break
                existing = db.scalar(select(Vacancy).where(
                    *_vacancy_scope(adapter_id, adapter.site_id, ref.external_id, session_id)
                ))
                if existing and (existing.session_id != session_id or existing.state not in {
                    "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING",
                }):
                    if existing.session_id != session_id:
                        search_metrics.record("overlap", {"external_id": ref.external_id})
                        search_metrics.flush(db, session_id)
                        db.commit()
                    continue
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
            except CaptchaRequired:
                raise
            except Exception:
                retry_needed = True
                with SessionLocal() as db:
                    self.emit(db, session_id, "vacancy_retry", "Чтение вакансии будет повторено", {"external_id": ref.external_id})
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
                    "SUBMITTING",
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

                if vacancy.state == "SUBMITTING":
                    verifier = getattr(adapter, "verify_submission", None)
                    if verifier is None:
                        raise RecoverableFailure("Адаптер не умеет проверять отправку")
                    verified = await verifier(executor.page)
                    if verified.status == "already_applied" and (vacancy.data or {}).get("submission_was_absent"):
                        verified = verified.model_copy(update={"status": "submitted"})
                    if verified.status in {"submitted", "already_applied"}:
                        self._record_submission(db, item, vacancy, verified)
                        continue
                    retry_check = getattr(adapter, "can_retry_application", None)
                    if not retry_check or not await retry_check(executor.page):
                        # Never click again on an ambiguous outcome. Other vacancies continue.
                        self._record_submission(db, item, vacancy, verified)
                        continue
                    vacancy.state = "READY_TO_SUBMIT"
                    db.commit()
                    # The site confirmed absence. Let other vacancies run before
                    # retrying a repeatedly failing form in the next pass.
                    retry_needed = True
                    continue

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
                             "criteria": ["tasks", "skills", "experience_depth", "role_match", "industry", "special_requirements"],
                            "minimum_scores": minimum_scores},
                        )
                        result = await evaluate(
                            posting,
                            profile,
                            selected_resumes,
                            gateway,
                            minimum_scores,
                            preference_policy,
                        )
                    except ModelUnavailable:
                        raise
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
                            "external_id": posting.external_id,
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
                                posting, profile, selected_resumes, gateway, preference_policy
                            )
                        except ModelUnavailable:
                            raise
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
                            retry_check = getattr(adapter, "can_retry_application", None)
                            absent = bool(retry_check and await retry_check(executor.page))
                            vacancy.data = {**(vacancy.data or {}), "submission_was_absent": absent}
                            # Opening HH's form can itself send a one-click application.
                            vacancy.state = "SUBMITTING"
                            db.commit()
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
                                summary_payload = {"job": {"title": vacancy.title, "description": getattr(posting, "description", "")}}
                                if preference_policy:
                                    summary_payload["preference_policy"] = preference_policy.model_dump(mode="json")
                                summary = await gateway.structured("job_summary", summary_payload, JobSummary)
                                short_description = summary.summary
                            except ModelUnavailable:
                                raise
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
                    except (ModelUnavailable, CaptchaRequired):
                        raise
                    except Exception:
                        # Keep SUBMITTING durable; reconcile it on the next attempt.
                        if adapter_id == "hirehi":
                            retry_needed = True
                            continue
                        raise
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
                        except CaptchaRequired:
                            raise
                        except Exception:
                            raise
                        if submission.status in {"unknown", "blocked"}:
                            raise RecoverableFailure("Ожидание подтверждения отклика")
                        self._record_submission(db, item, vacancy, submission)
                db.commit()
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                item.recovery = {**(item.recovery or {}), "attempt": 0, "retry_at": None, "message": None}
                db.commit()
            await asyncio.sleep(0.05)

        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                return
            if _limit_reached(_application_count(item, adapter_id), item.application_limit):
                completion_reason = _application_limit_reason(adapter_id)
            elif retry_needed:
                raise RecoverableFailure("Не все найденные вакансии обработаны; повторяем временные сбои")
        self.finalize(session_id, completion_reason)


workflow_manager = WorkflowManager()


def recover_orphaned_sessions() -> list[int]:
    """Resume accepted work after a process restart; leave drafts/CAPTCHA alone."""
    with SessionLocal() as db:
        recovered = list(db.scalars(select(JobSession.id).where(
            JobSession.status.in_((SessionStatus.RUNNING, SessionStatus.WAITING_FOR_LOGIN))
        )))
    for session_id in recovered:
        workflow_manager.launch(session_id)
    return recovered
