"""Private profile knowledge and durable, site-independent post-session questions."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select

from backend.intelligence.security import sanitize_untrusted_input
from backend.persistence.models import (
    ApplicationPlanRecord,
    JobSession,
    Notification,
    ProfileMemory,
    SessionQuestion,
    Vacancy,
    now,
)

TERMINAL = {"COMPLETED", "STOPPED", "FAILED"}
# Authentication material is never collected into applicant memory.
AUTH_QUESTION = re.compile(r"парол|password|одноразов\w*\s+код|код\s+(?:из|подтверждения)|otp\b|cookie|токен\s+доступа|api.?key", re.I)
CONTEXTUAL = re.compile(r"зарплат|зп\b|з/п|salary|compensation|доход|сумм|готов\w*.*(?:офис|переез|гибрид|удал|релокац)|relocat|work.*(?:office|hybrid)", re.I)


def normalized_question(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def can_remember(question: str) -> bool:
    safe_question = str(sanitize_untrusted_input(question, context="profile memory question") or "")
    if AUTH_QUESTION.search(safe_question):
        return False
    return bool(safe_question.strip())


def question_context(question: str, job: dict) -> dict:
    if not CONTEXTUAL.search(question):
        return {}
    return {key: job[key] for key in ("location", "work_format", "required_experience") if job.get(key)}


def _key(question: str, context: dict) -> str:
    text = normalized_question(question) + json.dumps(context, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _unanswered(plan: dict, vacancy: Vacancy):
    fields = plan.get("form_fields") or {}
    reasons = (vacancy.data or {}).get("application_review_reasons") or []
    for ident, reason in (plan.get("unanswered_fields") or {}).items():
        field = fields.get(ident) or {}
        label = field.get("label")
        if not label:
            # Old plans stored only ID -> reason; recover the exact question from the audit.
            suffix = ": " + reason
            label = next((text[:-len(suffix)] for text in reasons if text.endswith(suffix)), None)
        if label:
            yield label, reason, field.get("options") or []
    for label in (vacancy.data or {}).get("application_unanswered_questions") or []:
        yield label, "ИИ не смог ответить на вопрос анкеты", []


def collect_session_questions(db, item: JobSession) -> int:
    """Called after terminal transitions; idempotent across stop, recovery and restart."""
    if item.status not in TERMINAL or item.questions_collected_at is not None:
        return 0
    rows = db.execute(select(Vacancy, ApplicationPlanRecord).outerjoin(
        ApplicationPlanRecord, ApplicationPlanRecord.vacancy_id == Vacancy.id,
    ).where(Vacancy.session_id == item.id)).all()
    known = set(db.scalars(select(SessionQuestion.memory_key).where(SessionQuestion.session_id == item.id)))
    remembered = set(db.scalars(select(ProfileMemory.memory_key).where(ProfileMemory.profile_id == item.profile_id)))
    count = 0
    for vacancy, record in rows:
        for question, reason, options in _unanswered(record.data if record else {}, vacancy):
            safe_question = str(sanitize_untrusted_input(question, context="profile memory question") or "").strip()
            safe_reason = str(sanitize_untrusted_input(reason, context="profile memory reason") or "").strip()
            safe_options = sanitize_untrusted_input(options, context="profile memory options")
            if not safe_question:
                continue
            safe_vacancy_data = sanitize_untrusted_input(vacancy.data or {}, context="vacancy memory context")
            context = question_context(safe_question, safe_vacancy_data)
            key = _key(safe_question, context)
            if key in known or key in remembered:
                continue
            known.add(key)
            db.add(SessionQuestion(session_id=item.id, profile_id=item.profile_id, vacancy_id=vacancy.id,
                                   memory_key=key, question=safe_question, reason=safe_reason,
                                   options=safe_options, context=context))
            count += 1
    item.questions_collected_at = now()
    if count:
        db.add(Notification(source_type="session", source_id=str(item.id), target_path="/session",
                            kind="session_questions", title="Остались вопросы к вам",
                            message=f"Сессия #{item.id} завершена. Ответьте на вопросы ({count}), чтобы ИИ использовал ваши ответы в следующих анкетах."))
    db.flush()
    return count


def collect_finished_sessions(db) -> int:
    items = list(db.scalars(select(JobSession).where(JobSession.status.in_(TERMINAL), JobSession.questions_collected_at.is_(None))))
    return sum(collect_session_questions(db, item) for item in items)


def load_profile_memory(db, profile_id: int) -> list[dict]:
    """Internal API, deliberately independent of session and adapter identifiers."""
    result = []
    for entry in db.scalars(select(ProfileMemory).where(ProfileMemory.profile_id == profile_id).order_by(ProfileMemory.updated_at.desc(), ProfileMemory.id.desc())):
        if not isinstance(entry.question, str) or not isinstance(entry.answer, str) or not isinstance(entry.context or {}, dict):
            continue
        question = str(sanitize_untrusted_input(entry.question, context="stored profile question") or "").strip()
        answer = str(sanitize_untrusted_input(entry.answer, context="stored profile answer") or "").strip()
        context = sanitize_untrusted_input(entry.context or {}, context="stored profile context")
        if question and answer:
            result.append({"id": entry.id, "question": question, "answer": answer, "context": context})
    return result


def save_user_answer(db, question: SessionQuestion, answer: str) -> None:
    if not isinstance(question.question, str) or not isinstance(answer, str) or not isinstance(question.context or {}, dict):
        raise ValueError("Некорректные данные вопроса профиля")
    safe_question = str(sanitize_untrusted_input(question.question, context="profile memory question") or "").strip()
    safe_answer = str(sanitize_untrusted_input(answer, context="profile memory answer") or "").strip()
    safe_context = sanitize_untrusted_input(question.context or {}, context="profile memory context")
    if not can_remember(safe_question):
        raise ValueError("Пароли и коды доступа не сохраняются в ответах профиля")
    entry = db.scalar(select(ProfileMemory).where(ProfileMemory.profile_id == question.profile_id, ProfileMemory.memory_key == question.memory_key))
    if entry is None:
        entry = ProfileMemory(profile_id=question.profile_id, memory_key=question.memory_key,
                              question=safe_question, answer=safe_answer, context=safe_context)
        db.add(entry)
    else:
        entry.answer = safe_answer
        entry.updated_at = now()
    # The same question asked by another completed session needs no second interview.
    for pending in db.scalars(select(SessionQuestion).where(SessionQuestion.profile_id == question.profile_id,
                                                           SessionQuestion.memory_key == question.memory_key,
                                                           SessionQuestion.status == "pending")):
        pending.status = "answered"
        pending.answered_at = now()
    db.flush()
