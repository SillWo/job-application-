from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from backend.adapters import adapter_registry
from backend.browser.sessions import get_browser
from backend.intelligence.evaluator import _payload, evaluate
from backend.intelligence.gateway import ModelGateway, ModelUnavailable
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
    Report,
    ReviewItem,
    Vacancy,
    VacancySnapshot,
)
from backend.schemas import domain as domain_schemas
from backend.schemas.domain import (
    ApplicationPlan,
    JobEvaluation,
    SessionStatus,
)
from backend.services.reports import generate_report


def _increment_counter(db, item: JobSession, key: str, *, persist: bool = False) -> None:
    counters = dict(item.counters)
    counters[key] = counters.get(key, 0) + 1
    item.counters = counters
    if persist:
        db.commit()


def _limit_reached(count: int, limit: int | None) -> bool:
    """Unlimited session limits are represented by ``None``."""
    return limit is not None and count >= limit


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
    return getattr(domain_schemas, "PersonalProfileData", None) or domain_schemas.CandidateProfileData


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
    for key in (
        "id",
        "profile_id",
        "candidate_profile_id",
        "selected_for_matching",
        "resume_path",
        "file_path",
        "filename",
    ):
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
    raw_data = getattr(record, "data", None) or {}
    payload = {key: raw_data[key] for key in profile_keys if key in raw_data}
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

    raw_resumes = (profile_record.data or {}).get("resumes", [])
    return [
        resume
        for resume in raw_resumes
        if isinstance(resume, dict) and resume.get("selected_for_matching") is True
    ]


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
        "resume_path",
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

    def launch(self, session_id: int) -> None:
        task = self.tasks.get(session_id)
        if not task or task.done():
            self.tasks[session_id] = asyncio.create_task(self.run(session_id))

    def emit(
        self, db, session_id: int, event_type: str, message: str, data: dict | None = None
    ) -> None:
        db.add(
            BrowserEvent(
                session_id=session_id, event_type=event_type, message=message, data=data or {}
            )
        )
        db.commit()

    def ensure_terminal_report(self, db, item: JobSession) -> bool:
        """Create the partial report without changing or masking terminal state."""
        if db.scalar(select(Report).where(Report.session_id == item.id)):
            return True
        try:
            generate_report(db, item)
            return True
        except Exception as exc:
            db.rollback()
            try:
                self.emit(
                    db,
                    item.id,
                    "report_error",
                    f"Не удалось сформировать итоговый отчёт: {exc}",
                )
            except Exception:
                db.rollback()
            return False

    def finalize(self, session_id: int, completion_reason: str) -> None:
        """Finalize only active sessions; preserve externally terminal states."""
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item or item.status == SessionStatus.FAILED:
                if item:
                    self.ensure_terminal_report(db, item)
                return
            was_stopped = item.status == SessionStatus.STOPPED
            if not was_stopped:
                item.status = SessionStatus.COMPLETED
                item.stop_reason = completion_reason
            item.finished_at = datetime.now(timezone.utc)
            db.commit()
            report_created = self.ensure_terminal_report(db, item)
            message = (
                "Сессия завершена, отчёт сформирован"
                if report_created
                else "Сессия завершена, но отчёт сформировать не удалось"
            )
            self.emit(db, session_id, "session", message)

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
                    self.ensure_terminal_report(db, item)

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
                "already_applied": 0,
                "skipped_test": 0,
                "review": 0,
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
            score_threshold = item.score_threshold
            adapter_id, mode = item.adapter_id, item.mode

        adapter = adapter_registry.get(adapter_id)
        executor = get_browser(session_id)
        if not executor:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.WAITING_FOR_LOGIN
                item.stop_reason = "Откройте отдельный Chromium и войдите в HH.ru вручную"
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

        search_filters = {"query": _resume_desired_title(selected_resumes[0])}
        await adapter.open_search(executor.page, search_filters)
        blockers = await adapter.detect_blockers(executor.page)
        if blockers:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                item.status = SessionStatus.PAUSED
                item.stop_reason = blockers[0].message
                db.commit()
                self.emit(db, session_id, "human_required", blockers[0].message)
            return
        refs = await adapter.collect_job_refs(executor.page)
        collect_more = getattr(adapter, "collect_more_job_refs", None)
        seen_ref_ids = {ref.external_id for ref in refs}
        if not refs and collect_more is not None:
            refs.extend(await collect_more(executor.page))
            seen_ref_ids.update(ref.external_id for ref in refs)
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

        gateway = ModelGateway()
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
                if _limit_reached(item.counters.get("viewed", 0), item.viewed_limit) or _limit_reached(
                    item.counters.get("submitted", 0), item.application_limit
                ):
                    if _limit_reached(item.counters.get("viewed", 0), item.viewed_limit):
                        completion_reason = "Достигнут лимит просмотра вакансий"
                    else:
                        completion_reason = "Достигнут лимит отправленных откликов"
                    collect_more = None
                    return False
            next_refs = await collect_more(executor.page)
            while not next_refs and not getattr(adapter, "search_exhausted", True):
                next_refs = await collect_more(executor.page)
            new_refs = [
                candidate
                for candidate in next_refs
                if candidate.external_id not in seen_ref_ids
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
                if _limit_reached(item.counters.get("submitted", 0), application_limit):
                    completion_reason = "Достигнут лимит отправленных откликов"
                    break
            try:
                await adapter.open_job(executor.page, ref)
                blockers = await adapter.detect_blockers(executor.page)
                if blockers:
                    with SessionLocal() as db:
                        item = db.get(JobSession, session_id)
                        item.status = SessionStatus.PAUSED
                        item.stop_reason = blockers[0].message
                        db.commit()
                        self.emit(db, session_id, "human_required", blockers[0].message)
                    return
                posting = await adapter.extract_job(executor.page)
            except Exception as exc:
                with SessionLocal() as db:
                    item = db.get(JobSession, session_id)
                    counters = dict(item.counters)
                    counters["errors"] += 1
                    item.counters = counters
                    self.emit(
                        db, session_id, "browser_error", f"Не удалось прочитать вакансию: {exc}"
                    )
                continue
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                existing = db.scalar(
                    select(Vacancy).where(
                        Vacancy.source == posting.source, Vacancy.external_id == posting.external_id
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
                        result = await evaluate(
                            posting,
                            profile,
                            selected_resumes,
                            score_threshold,
                            gateway,
                        )
                    except ModelUnavailable as exc:
                        item.status = SessionStatus.PAUSED
                        db.commit()
                        self.emit(db, session_id, "model_unavailable", str(exc))
                        return
                db.refresh(item)
                if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                    return
                if evaluation_record is None:
                    db.add(Evaluation(vacancy_id=vacancy.id, data=result.model_dump()))
                    self.emit(
                        db,
                        session_id,
                        "evaluation",
                        f"Оценка {result.score}/100, порог {score_threshold}",
                        {
                            "vacancy_id": vacancy.id,
                            "score": result.score,
                            "threshold": score_threshold,
                            "decision": result.decision,
                            "breakdown": [row.model_dump() for row in result.score_breakdown],
                        },
                    )
                if result.decision == "skip":
                    vacancy.state = "REJECTED_BY_MODEL"
                    counters = dict(item.counters)
                    counters["filtered"] += 1
                    item.counters = counters
                elif result.decision == "manual_review":
                    vacancy.state = "NEEDS_REVIEW"
                    db.add(
                        ReviewItem(
                            session_id=session_id,
                            vacancy_id=vacancy.id,
                            kind="evaluation",
                            question=result.reason,
                        )
                    )
                    counters = dict(item.counters)
                    counters["review"] += 1
                    item.counters = counters
                else:
                    if evaluation_record is None:
                        _increment_counter(db, item, "matched", persist=True)
                    # Persist before awaiting the model. A later refresh would
                    # otherwise discard the dirty JSON counter value.
                    plan = ApplicationPlan(
                        vacancy_id=vacancy.id,
                        resume_file=resume_file,
                        unknown_question_policy="manual_review",
                        submission_allowed=mode == "autopilot",
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
                            item.status = SessionStatus.PAUSED
                            item.stop_reason = str(exc)
                            db.commit()
                            self.emit(db, session_id, "model_unavailable", str(exc))
                            return
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                        return
                    plan.cover_letter = letter
                    plan_record.data = plan.model_dump()
                    if cover_record is None:
                        db.add(CoverLetter(vacancy_id=vacancy.id, text=letter))
                    vacancy.state = "READY_TO_SUBMIT"
                if vacancy.state == "READY_TO_SUBMIT" and mode == "analysis_only":
                    vacancy.state = "NEEDS_REVIEW"
                    db.add(
                        ReviewItem(
                            session_id=session_id,
                            vacancy_id=vacancy.id,
                            kind="analysis_only",
                            question="В analysis_only отправка отключена",
                        )
                    )
                    counters = dict(item.counters)
                    counters["review"] += 1
                    item.counters = counters
                elif vacancy.state == "READY_TO_SUBMIT" and mode == "review_before_submit":
                    vacancy.state = "NEEDS_REVIEW"
                    db.add(
                        ReviewItem(
                            session_id=session_id,
                            vacancy_id=vacancy.id,
                            kind="submission",
                            question="Подтвердите подготовленный отклик",
                        )
                    )
                    counters = dict(item.counters)
                    counters["review"] += 1
                    item.counters = counters
                elif vacancy.state == "READY_TO_SUBMIT" and mode == "autopilot":
                    db.commit()
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                        return
                    blockers = await adapter.detect_blockers(executor.page)
                    if blockers:
                        item.status = SessionStatus.PAUSED
                        item.stop_reason = blockers[0].message
                        self.emit(db, session_id, "human_required", blockers[0].message)
                        return
                    try:
                        form = await adapter.open_application(executor.page)
                        result = await adapter.fill_application(executor.page, plan)
                    except Exception as exc:
                        vacancy.state = "NEEDS_REVIEW"
                        db.add(
                            ReviewItem(
                                session_id=session_id,
                                vacancy_id=vacancy.id,
                                kind="unstable_application_form",
                                question=(
                                    "HH.ru изменил страницу формы во время чтения. "
                                    f"Проверьте отклик вручную: {exc}"
                                ),
                            )
                        )
                        counters = dict(item.counters)
                        counters["review"] += 1
                        item.counters = counters
                        self.emit(
                            db,
                            session_id,
                            "human_required",
                            "Форма HH.ru изменилась во время чтения; вакансия передана на проверку",
                            {"vacancy_id": vacancy.id},
                        )
                        db.commit()
                        continue
                    questions = unresolved_application_questions(form, result)
                    if questions:
                        vacancy.state = "NEEDS_REVIEW"
                        db.add(
                            ReviewItem(
                                session_id=session_id,
                                vacancy_id=vacancy.id,
                                kind="unknown_question",
                                question="; ".join(questions),
                            )
                        )
                        counters = dict(item.counters)
                        counters["review"] += 1
                        item.counters = counters
                    else:
                        db.refresh(item)
                        if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                            return
                        submission = await adapter.submit_application(executor.page)
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
            item.stop_reason = (
                "Сессия прервана перезапуском платформы; запустите новую сессию"
            )
            item.finished_at = datetime.now(timezone.utc)
            workflow_manager.emit(
                db,
                item.id,
                "recovery",
                item.stop_reason,
            )
            workflow_manager.ensure_terminal_report(db, item)
            recovered.append(item.id)
    return recovered


def backfill_terminal_reports() -> list[int]:
    """Create missing reports for terminal sessions from earlier releases."""
    created: list[int] = []
    with SessionLocal() as db:
        terminal = list(
            db.scalars(
                select(JobSession).where(
                    JobSession.status.in_(
                        (SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.FAILED)
                    ),
                    ~select(Report.id)
                    .where(Report.session_id == JobSession.id)
                    .exists(),
                )
            )
        )
        for item in terminal:
            if workflow_manager.ensure_terminal_report(db, item):
                created.append(item.id)
    return created
