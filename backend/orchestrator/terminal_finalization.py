"""Terminal session cleanup shared by the workflow and API supervisor."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from backend.persistence.models import (
    ApplicationPlanRecord,
    BrowserEvent,
    Evaluation,
    JobSession,
    SessionResumeSnapshot,
    Vacancy,
)
from backend.schemas.domain import SessionStatus
from backend.services import search_metrics
from backend.services.hirehi_reporting import write_session_pdf
from backend.services.private_text import _unseal_private, render_local_private

_QUESTION_BEARING_DATA_KEYS = frozenset({
    "answer", "answers", "field", "fields", "form_answers", "form_fields",
    "known_answers", "question", "questions", "unanswered", "unanswered_fields",
    "unresolved", "application_error_reasons", "application_unanswered_questions",
})
_PENDING_VACANCY_STATES = {
    "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING",
}
_TERMINAL_STATUSES = {
    SessionStatus.STOPPED, SessionStatus.CANCELLED,
    SessionStatus.FAILED, SessionStatus.COMPLETED,
}


def _is_question_bearing_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
    return (
        normalized in _QUESTION_BEARING_DATA_KEYS
        or "question" in normalized or "answer" in normalized
        or "unanswered" in normalized or "unresolved" in normalized
        or normalized.startswith("form_field")
    )


def _collect_question_literals(value: Any, *, key_hint: Any = None) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_question_bearing_key(key):
                result.update(_collect_question_literals(child, key_hint=key))
            elif _is_question_bearing_key(key_hint):
                if (
                    isinstance(key, str)
                    and str(key_hint).casefold() in {"known_answers", "form_answers"}
                    and len(key.strip()) >= 3
                ):
                    result.add(key.strip())
                result.update(_collect_question_literals(child, key_hint=key_hint))
            else:
                result.update(_collect_question_literals(child, key_hint=key_hint))
    elif isinstance(value, (list, tuple)):
        for child in value:
            result.update(_collect_question_literals(child, key_hint=key_hint))
    elif isinstance(value, str) and _is_question_bearing_key(key_hint):
        literal = value.strip()
        if len(literal) >= 3:
            result.add(literal)
    return result


def _scrub_question_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_question_data(child)
            for key, child in value.items()
            if not _is_question_bearing_key(key)
        }
    if isinstance(value, list):
        return [_scrub_question_data(child) for child in value]
    return value


def _replace_question_literals(value: Any, literals: set[str], *, key_hint: Any = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_question_literals(child, literals, key_hint=key)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_question_literals(child, literals, key_hint=key_hint) for child in value]
    if isinstance(value, str) and literals and _is_question_bearing_key(key_hint):
        result = value
        for literal in sorted(literals, key=len, reverse=True):
            result = result.replace(literal, "[удалено]")
        return result
    return value


def scrub_snapshot_question_artifacts(db, session_id: int) -> None:
    """Remove question-bearing recovery artifacts while retaining the snapshot."""
    snapshot = db.scalar(
        select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
    )
    if snapshot is None:
        return
    item = db.get(JobSession, session_id)
    if item is None:
        return
    vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == session_id)))
    records = list(db.scalars(
        select(ApplicationPlanRecord).where(
            ApplicationPlanRecord.vacancy_id.in_([vacancy.id for vacancy in vacancies] or [-1])
        )
    ))
    events = list(db.scalars(select(BrowserEvent).where(BrowserEvent.session_id == session_id)))
    literals: set[str] = set()
    literals.update(_collect_question_literals(item.recovery))
    for vacancy in vacancies:
        literals.update(_collect_question_literals(vacancy.data))
    for record in records:
        literals.update(_collect_question_literals(record.data))
    for event in events:
        literals.update(_collect_question_literals(event.data))
    for record in records:
        record.data = _scrub_question_data(record.data or {})
    for vacancy in vacancies:
        vacancy.data = _replace_question_literals(_scrub_question_data(vacancy.data or {}), literals)
    recovery = _scrub_question_data(item.recovery or {})
    for key in (
        "pending_refs", "pending_questions", "manual_application_vacancy_ids",
        "retry_at", "message",
    ):
        recovery.pop(key, None)
    item.recovery = recovery
    for event in events:
        event_literals = _collect_question_literals(event.data)
        event.data = _scrub_question_data(event.data or {})
        if isinstance(event.message, str) and event_literals:
            event.message = _replace_question_literals(
                event.message, event_literals, key_hint="question"
            )


def _write_hirehi_report(db, session_id: int) -> int:
    item = db.get(JobSession, session_id)
    if item is None or item.adapter_id != "hirehi":
        return 0
    snapshot = db.scalar(
        select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
    )
    report_private = _unseal_private(snapshot.private_view) if snapshot is not None else {}
    joined_rows = db.execute(
        select(Vacancy, Evaluation)
        .outerjoin(Evaluation, Evaluation.vacancy_id == Vacancy.id)
        .where(Vacancy.session_id == session_id)
    ).all()
    report_rows = []
    for vacancy, evaluation in joined_rows:
        data = vacancy.data or {}
        if not data.get("report_route_kind"):
            continue
        report_letter = data.get("report_cover_letter", "")
        if report_letter:
            report_letter = render_local_private(report_letter, report_private)
        report_rows.append({
            "title": vacancy.title, "company": vacancy.company or "",
            "score": (evaluation.data or {}).get("score") if evaluation else None,
            "hirehi_url": data.get("report_hirehi_url", vacancy.url),
            "route_kind": data.get("report_route_kind", ""),
            "target_url": data.get("report_target_url", ""),
            "contact": data.get("report_contact", ""),
            "short_description": data.get("report_short_description", ""),
            "cover_letter": report_letter,
        })
    write_session_pdf(session_id, report_rows)
    existing = db.scalar(select(BrowserEvent.id).where(
        BrowserEvent.session_id == session_id,
        BrowserEvent.event_type == "report_ready",
    ).limit(1))
    if existing is None:
        db.add(BrowserEvent(
            session_id=session_id,
            event_type="report_ready",
            message="PDF отчёт сформирован",
            data={"path": f"/api/sessions/{session_id}/report/pdf", "count": len(report_rows)},
        ))
    return len(report_rows)


def terminalize_pending_vacancies(db, item: JobSession) -> int:
    status = item.status
    if status not in _TERMINAL_STATUSES:
        return 0
    if status == SessionStatus.CANCELLED:
        code, message = "SESSION_CANCELLED", "Вакансия не обработана: сессия отменена пользователем"
    elif status == SessionStatus.STOPPED:
        code, message = "SESSION_STOPPED", "Вакансия не обработана: сессия остановлена пользователем"
    elif status == SessionStatus.FAILED:
        code, message = "SESSION_FAILED", "Вакансия не обработана: сессия завершилась с ошибкой"
    else:
        code, message = "VACANCY_PROCESSING_FAILED", "Вакансия не обработана до завершения сессии"
    vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == item.id)))
    changed = 0
    for vacancy in vacancies:
        if vacancy.state not in _PENDING_VACANCY_STATES:
            continue
        data = dict(vacancy.data or {})
        if status == SessionStatus.CANCELLED:
            data.update(cancellation_code=code, cancellation_message=message)
            vacancy.state = "CANCELLED"
        else:
            data.update(error_code=code, error_message=message)
            vacancy.state = "ERROR"
            counters = dict(item.counters or {})
            counters["errors"] = counters.get("errors", 0) + 1
            item.counters = counters
        vacancy.data = data
        changed += 1
    return changed


def finalize_terminal_session(db, item: JobSession, *, report_writer=None) -> bool:
    """Idempotently apply common cleanup after a terminal status is durable."""
    if item.status not in _TERMINAL_STATUSES:
        return False
    recovery = dict(item.recovery or {})
    if recovery.get("terminal_finalized"):
        return False
    if item.finished_at is None:
        item.finished_at = datetime.now(timezone.utc)
    if item.adapter_id == "hirehi":
        if report_writer is None:
            _write_hirehi_report(db, item.id)
        else:
            report_writer(db, item.id)
    terminalize_pending_vacancies(db, item)
    scrub_snapshot_question_artifacts(db, item.id)
    search_metrics.freeze(db, item)
    recovery = dict(item.recovery or {})
    for key in (
        "pending_refs", "pending_questions", "manual_application_vacancy_ids",
        "retry_at", "message",
    ):
        recovery.pop(key, None)
    recovery["terminal_finalized"] = True
    item.recovery = recovery
    db.commit()
    return True
