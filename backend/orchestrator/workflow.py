from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Any

from sqlalchemy import select

from backend.adapters import adapter_registry
from backend.adapters.base.protocol import JobRef
from backend.browser.sessions import close_browser, get_browser, restore_browser
from backend.intelligence.adaptive_search_planner import plan_portfolio
from backend.intelligence.evaluator import _payload, evaluate
from backend.intelligence.gateway import ModelGateway, ModelUnavailable
from backend.intelligence.hirehi_category import JobSummary, choose_hirehi_category
from backend.intelligence.hirehi_grade import hirehi_grades
from backend.intelligence.letter_writer import (
    CoverLetterValidationError,
    validate_cover_letter,
    write_cover_letter,
)
from backend.intelligence.preference_policy import compile_preference_policy
from backend.intelligence.search_planner import plan_search_queries
from backend.intelligence.security import (
    PromptInjectionDetected,
    assert_safe_outgoing_text,
    assert_safe_output,
    sanitize_untrusted_input,
)
from backend.orchestrator.adaptive_search import AdaptiveSearch
from backend.orchestrator.application_guard import unresolved_application_questions
from backend.orchestrator.hh_application import complete_application
from backend.orchestrator.recovery import (
    AuthenticationPending,
    CaptchaRequired,
    RecoverableFailure,
    RecoveryAdapter,
)
from backend.persistence.database import SessionLocal
from backend.persistence.models import (
    Application,
    ApplicationPlanRecord,
    BrowserEvent,
    CoverLetter,
    Evaluation,
    JobSession,
    SessionResumeSnapshot,
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
from backend.services.resume_session import (
    _sha256,
    _unseal_private,
    delete_snapshot,
    full_resume_model_payload,
    professional_view,
    render_local_private,
)


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


_VACANCY_ERROR_CODES = {
    "APPLICATION_FORM_UNRESOLVED",
    "APPLICATION_FORM_UNSUPPORTED",
    "APPLICATION_FORM_STUCK",
    "FOREIGN_APPLICATION_CONFIRMATION_FAILED",
    "UNKNOWN_APPLICATION_ROUTE",
    "SUBMISSION_BLOCKED",
    "MFA_REQUIRED",
    "SECURITY_BLOCKED",
    "SITE_ACCESS_BLOCKED",
    "VACANCY_PROCESSING_FAILED",
}


def _record_vacancy_error(
    item: JobSession, vacancy: Vacancy, error_code: str, error_message: str,
) -> None:
    """Persist one safe, user-readable error outcome for a vacancy."""
    if error_code not in _VACANCY_ERROR_CODES:
        error_code = "VACANCY_PROCESSING_FAILED"
    try:
        cleaned = sanitize_untrusted_input(str(error_message), context="vacancy error message")
    except PromptInjectionDetected:
        cleaned = "Вакансия не обработана из-за небезопасных данных"
    if not isinstance(cleaned, str) or not cleaned.strip():
        cleaned = "Вакансия не обработана из-за ошибки"
    data = dict(vacancy.data or {})
    data["error_code"] = error_code
    data["error_message"] = cleaned[:1000]
    vacancy.data = data
    previous_state = vacancy.state
    vacancy.state = "ERROR"
    if previous_state != "ERROR":
        counters = dict(item.counters or {})
        counters["errors"] = counters.get("errors", 0) + 1
        item.counters = counters


def _record_blocker_outcome(item: JobSession, blocker: Any, vacancy: Vacancy) -> None:
    if blocker.kind == "test":
        counters = dict(item.counters or {})
        counters["filtered"] = counters.get("filtered", 0) + 1
        item.counters = counters
        vacancy.state = "REJECTED_BY_MODEL"
        return
    codes = {
        "unknown_form": "APPLICATION_FORM_UNSUPPORTED",
        "mfa": "MFA_REQUIRED",
        "blocked": "SITE_ACCESS_BLOCKED",
        "sensitive": "APPLICATION_FORM_UNSUPPORTED",
    }
    _record_vacancy_error(
        item, vacancy, codes.get(blocker.kind, "VACANCY_PROCESSING_FAILED"),
        str(getattr(blocker, "message", "Форма вакансии не поддерживается автоматически")),
    )


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


_COVER_LETTER_RETRY_LIMIT = 3
_SUBMISSION_RECONCILIATION_LIMIT = 3
_SESSION_RECOVERY_RETRY_LIMIT = 8
_EVALUATION_SECURITY_VERSION = 1
_RESUME_HASH_KEY = "_resume_content_hash"
_COVER_LETTER_HASH_KEY = "cover_letter_content_hash"
def _cache_matches_resume(data: Any, content_hash: str | None) -> bool:
    """Only session-snapshot artifacts may be reused by a snapshot session."""
    if not isinstance(data, dict):
        return content_hash is None
    cached_hash = data.get(_RESUME_HASH_KEY)
    return cached_hash == content_hash if content_hash else cached_hash is None


def _private_mapping(private: Any) -> dict[str, str]:
    result: dict[str, str] = {}

    def walk(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            if "value" in value and "availability" in value:
                if value.get("availability") == "present":
                    walk(value.get("value"), key)
                return
            for name, child in value.items():
                normalized = str(name).casefold()
                if normalized in {"full_name", "name", "fio"}:
                    walk(child, "full_name")
                elif normalized in {"phone", "email", "messengers"}:
                    walk(child, normalized)
                elif normalized in {"identity", "contacts"}:
                    walk(child)
        elif isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if items and key:
                result[key] = ", ".join(items)
        elif value is not None and key and str(value).strip():
            result[key] = str(value).strip()

    walk(private)
    return result


def _redact_private_string(value: str, private: Any) -> str:
    result = str(value or "")
    mapping = _private_mapping(private)
    for key, literal in sorted(mapping.items(), key=lambda pair: len(pair[1]), reverse=True):
        if len(literal) >= 2:
            placeholder = "full_name" if key in {"name", "fio"} else key
            result = re.sub(re.escape(literal), "{{" + placeholder + "}}", result, flags=re.IGNORECASE)
    result = re.sub(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])", "{{email}}", result)
    result = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "{{phone}}", result)
    return result


def _redact_plan(plan: ApplicationPlan, private: Any) -> dict[str, Any]:
    """Serialize a plan without retaining values locally bound for a form."""
    data = plan.model_dump(mode="json")

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        return _redact_private_string(value, private) if isinstance(value, str) else value

    return redact(data)


def _snapshot_content_hash(snapshot: SessionResumeSnapshot) -> str:
    """Rebuild the complete snapshot from public + sealed parts and hash it."""
    if not snapshot.source_site or not snapshot.content_hash:
        raise ValueError("Повреждён временный снимок резюме")
    try:
        private = _unseal_private(snapshot.private_view)
        payload = dict(snapshot.snapshot or {})
        payload["identity"] = private.get("identity", {})
        payload["contacts"] = private.get("contacts", {})
        rebuilt = domain_schemas.SiteResumeSnapshot.model_validate(payload)
        canonical = rebuilt.model_dump(
            mode="json", exclude={"content_hash", "imported_at", "source_updated_at"}
        )
        digest = _sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except Exception as exc:
        raise ValueError("Повреждён временный снимок резюме") from exc
    # ``issue_preview_token`` intentionally redacts private values duplicated
    # in professional prose before persisting the public snapshot.  Validate
    # that this redacted projection still matches the sealed snapshot, then
    # accept the original immutable hash for that historical representation.
    public_projection = professional_view(rebuilt).model_dump(mode="json")
    stored_projection = snapshot.professional_view or {}
    projection_ok = json.dumps(public_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        stored_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    stored_hash = (snapshot.snapshot or {}).get("content_hash")
    if (digest != snapshot.content_hash and not (projection_ok and stored_hash == snapshot.content_hash)) or rebuilt.source_site != snapshot.source_site:
        raise ValueError("Повреждён временный снимок резюме")
    return snapshot.content_hash


def _snapshot_model_payload(
    snapshot: SessionResumeSnapshot, private_context: dict[str, Any]
) -> dict[str, Any]:
    """Return the complete model resume for current and legacy snapshots."""
    full_snapshot = snapshot.full_snapshot
    if not isinstance(full_snapshot, dict):
        full_snapshot = dict(snapshot.snapshot or {})
        full_snapshot["identity"] = private_context.get("identity", {})
        full_snapshot["contacts"] = private_context.get("contacts", {})
    return full_resume_model_payload(full_snapshot)


_QUESTION_BEARING_DATA_KEYS = frozenset({
    "answer",
    "answers",
    "field",
    "fields",
    "form_answers",
    "form_fields",
    "known_answers",
    "question",
    "questions",
    "unanswered",
    "unanswered_fields",
    "unresolved",
    "application_error_reasons",
    "application_unanswered_questions",
})


def _is_question_bearing_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
    return (
        normalized in _QUESTION_BEARING_DATA_KEYS
        or "question" in normalized
        or "answer" in normalized
        or "unanswered" in normalized
        or "unresolved" in normalized
        or normalized.startswith("form_field")
    )


def _collect_question_literals(value: Any, *, key_hint: Any = None) -> set[str]:
    """Collect question/answer strings before removing their durable containers."""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_question_bearing_key(key):
                result.update(_collect_question_literals(child, key_hint=key))
            # Mapping keys in known_answers/form_answers can themselves be
            # literal questions.  Do not treat generic keys such as
            # ``question`` or ``answers`` as literals.
            elif _is_question_bearing_key(key_hint):
                if (
                    isinstance(key, str)
                    and str(key_hint).casefold()
                    in {"known_answers", "form_answers"}
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
    """Drop question-bearing keys while preserving unrelated report fields."""
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
    """Remove copies of a known question/answer from residual event text."""
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


def _scrub_snapshot_question_artifacts(db, session_id: int) -> None:
    """Erase question artifacts only for immutable-snapshot sessions."""
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
        # Event messages are ordinary audit/recovery text by default.  Only
        # events carrying an explicit question-bearing data container may
        # authorize removal of a matching literal from that message.
        literals.update(_collect_question_literals(event.data))

    for record in records:
        record.data = _scrub_question_data(record.data or {})
    for vacancy in vacancies:
        vacancy.data = _replace_question_literals(
            _scrub_question_data(vacancy.data or {}), literals
        )
    recovery = _scrub_question_data(item.recovery or {})
    recovery.pop("manual_application_vacancy_ids", None)
    recovery.pop("pending_questions", None)
    item.recovery = recovery
    for event in events:
        event_literals = _collect_question_literals(event.data)
        event.data = _scrub_question_data(event.data or {})
        if isinstance(event.message, str) and event_literals:
            event.message = _replace_question_literals(
                event.message, event_literals, key_hint="question"
            )


def _security_incident(exc: PromptInjectionDetected, *, context: str) -> dict[str, str]:
    """Build a bounded, attack-content-free incident record."""
    reason_code = getattr(exc, "reason_code", "prompt_injection_detected")
    if not isinstance(reason_code, str) or not reason_code.isascii():
        reason_code = "prompt_injection_detected"
    reason_code = "".join(char for char in reason_code if char.isalnum() or char in "_-")[:80]
    return {"reason_code": reason_code or "prompt_injection_detected", "context": context[:80]}


def _record_security_incident(
    db, item: JobSession, vacancy: Vacancy, exc: PromptInjectionDetected, *, context: str,
    emit=None,
) -> None:
    """Stop one vacancy safely without exposing the untrusted text."""
    incident = _security_incident(exc, context=context)
    data = dict(vacancy.data or {})
    already_recorded = bool(data.get("security_incident_recorded"))
    _record_vacancy_error(
        item, vacancy, "SECURITY_BLOCKED",
        "Обработка вакансии остановлена из-за небезопасного содержимого",
    )
    data = dict(vacancy.data or {})
    data["security_incident"] = incident
    if not already_recorded:
        data["security_incident_recorded"] = True
    vacancy.data = data
    if emit:
        emit(
            db, item.id, "security_skipped", "Вакансия пропущена из-за небезопасного содержимого", {
            "vacancy_id": vacancy.id,
            **incident,
            }
        )


def _assert_safe_application_plan(
    plan: ApplicationPlan,
    profile: Any,
    resumes: list[Any],
    *,
    context: str,
    source_form: Any = None,
) -> None:
    """Recheck generated values while allowing source form metadata to persist.

    ``form_fields`` and ``FormAnswer.field`` are copied from the employer form
    so adapters can bind the answer to the exact live field. Those labels and
    options are untrusted source metadata, not model instructions or free text
    generated for submission, so scanning the whole plan would reject a valid
    binding merely because its source label contains instruction-like text.
    """
    if plan.cover_letter:
        assert_safe_outgoing_text(plan.cover_letter, profile, resumes, context=f"{context}_letter")
    current_options = {
        field.id: set(field.options)
        for field in getattr(source_form, "fields", [])
    }
    for answer in plan.form_answers.values():
        for value in answer.values:
            # A fixed-choice answer is allowed when it is byte-for-byte one of
            # the current employer form's original options. Before the live
            # form is available, defer exact cached options until that check.
            allowed_options = current_options.get(answer.field.id)
            if allowed_options is None and source_form is None:
                allowed_options = set(answer.field.options)
            if allowed_options is not None and value in allowed_options:
                continue
            assert_safe_outgoing_text(value, profile, resumes, context=f"{context}_answer")
    for value in plan.known_answers.values():
        assert_safe_outgoing_text(value, profile, resumes, context=f"{context}_known_answer")


def _resume_desired_title(resume: dict) -> str:
    for key in ("desired_title", "title", "position"):
        value = resume.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("target", "general", "general_info", "common"):
        nested = resume.get(key)
        if isinstance(nested, dict):
            title = _resume_desired_title(nested)
            if title:
                return title
    return ""


class WorkflowManager:
    retry_base_seconds = 5
    retry_max_seconds = 300

    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.site_leases: dict[str, int] = {}
        self.task_sites: dict[int, str] = {}
        # launch() is called synchronously by the API before the async task is
        # created. Serialize the check and in-memory lease claim.
        self._launch_lock = Lock()

    def launch(self, session_id: int) -> bool | None:
        with self._launch_lock:
            task = self.tasks.get(session_id)
            if task and not task.done():
                return None
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if not item:
                    return False
                site_id = item.adapter_id
                active_statuses = (SessionStatus.RUNNING, SessionStatus.PAUSED)
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
                # Claim the DB row before creating the task; otherwise two
                # same-site requests could both pass the check in the gap
                # before the API's old post-launch status update.
                item.status = SessionStatus.RUNNING
                db.commit()
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
        snapshot = db.scalar(
            select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
        )
        report_private = _unseal_private(snapshot.private_view) if snapshot is not None else {}
        vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == session_id)))
        vacancies = [v for v in vacancies if (v.data or {}).get("report_route_kind")]
        rows = []
        for vacancy in vacancies:
            data = vacancy.data or {}
            evaluation = db.scalar(select(Evaluation).where(Evaluation.vacancy_id == vacancy.id))
            report_letter = data.get("report_cover_letter", "")
            if report_letter:
                # Render only the in-memory report row immediately before PDF
                # generation. The placeholder-bearing vacancy JSON remains
                # safe and is never overwritten with the rendered copy.
                report_letter = render_local_private(report_letter, report_private)
            rows.append({
                "title": vacancy.title, "company": vacancy.company or "",
                "score": (evaluation.data or {}).get("score") if evaluation else None,
                "hirehi_url": data.get("report_hirehi_url", vacancy.url),
                "route_kind": data.get("report_route_kind", ""),
                "target_url": data.get("report_target_url", ""),
                "contact": data.get("report_contact", ""),
                "short_description": data.get("report_short_description", ""),
                "cover_letter": report_letter,
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
            _scrub_snapshot_question_artifacts(db, session_id)
            if not was_stopped:
                item.status = SessionStatus.COMPLETED
                item.stop_reason = completion_reason
            item.recovery = {
                **(item.recovery or {}),
                "pending_refs": [],
                "pending_questions": [],
                "manual_application_vacancy_ids": [],
                "retry_at": None,
                "message": None,
            }
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
                    # Snapshot sessions must lose question-bearing artifacts
                    # before terminal cleanup.
                    _scrub_snapshot_question_artifacts(db, final_item.id)
                    search_metrics.freeze(db, final_item)
                    recovery = dict(final_item.recovery or {})
                    recovery.pop("pending_questions", None)
                    recovery.pop("manual_application_vacancy_ids", None)
                    final_item.recovery = recovery
                    # Cleanup happens only after report/metrics finalization,
                    # and remains idempotent for retries and legacy sessions.
                    delete_snapshot(db, final_item.id)
                    db.commit()
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
            if isinstance(exc, PromptInjectionDetected):
                # A security failure outside the per-vacancy boundary is
                # terminal and must never become a model-unavailable retry.
                item.status = SessionStatus.FAILED
                item.stop_reason = "Обнаружено небезопасное содержимое"
                self.emit(
                    db,
                    session_id,
                    "security_failed",
                    "Сессия остановлена из-за небезопасного содержимого",
                    {"kind": "prompt_injection"},
                )
                db.commit()
                return False
            recovery = dict(item.recovery or {})
            try:
                previous_attempt = max(0, int(recovery.get("attempt", 0)))
            except (TypeError, ValueError):
                previous_attempt = 0
            if previous_attempt >= _SESSION_RECOVERY_RETRY_LIMIT:
                item.status = SessionStatus.FAILED
                item.stop_reason = "Сессия остановлена после исчерпания повторов временной ошибки"
                item.finished_at = datetime.now(timezone.utc)
                self.emit(
                    db, session_id, "session_failed", item.stop_reason,
                    {"kind": "recovery_exhausted", "attempts": previous_attempt},
                )
                db.commit()
                return False
            attempt = previous_attempt + 1
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

    def _record_unconfirmed_submission(
        self, db, item, vacancy, *, message: str,
    ) -> None:
        """Persist a bounded reconciliation failure without calling it an error.

        ``UNCONFIRMED`` means the site never proved whether the click took
        effect.  It is intentionally distinct from a confirmed blocker and
        from a workflow error, and creates one durable ``unknown`` application
        row so a restart cannot click again or count it twice.
        """
        data = dict(vacancy.data or {})
        data["error_code"] = "SUBMISSION_UNCONFIRMED"
        data["error_message"] = (
            "Не удалось подтвердить отправку отклика после нескольких попыток"
        )
        vacancy.data = data
        vacancy.state = "UNCONFIRMED"
        existing = db.scalar(select(Application).where(Application.vacancy_id == vacancy.id))
        if existing is None:
            db.add(
                Application(
                    vacancy_id=vacancy.id,
                    status="unknown",
                )
            )
            counters = dict(item.counters or {})
            counters["errors"] = counters.get("errors", 0) + 1
            item.counters = counters
        elif existing.status != "unknown":
            existing.status = "unknown"
        self.emit(
            db, item.id, "submission", message,
            {"vacancy_id": vacancy.id, "status": "unknown"},
        )

    def _record_submission(self, db, item, vacancy, submission) -> bool:
        """Persist a submission outcome.

        An ``unknown``/``blocked`` transport result is not terminal: the
        browser may have accepted the click while the confirmation rendered
        late.  Keep the durable row in ``SUBMITTING`` and let the next pass
        reconcile it.  The bounded counter is stored on the vacancy so a
        restart cannot turn this into an unbounded retry loop.
        """
        transport_status = submission.status
        if transport_status == "blocked" and bool(getattr(submission, "confirmed", False)):
            _record_vacancy_error(
                item, vacancy, "SUBMISSION_BLOCKED", submission.message,
            )
            return False
        if transport_status in {"unknown", "blocked"}:
            data = dict(vacancy.data or {})
            try:
                attempts = max(0, int(data.get("submission_reconciliation_attempts", 0)))
            except (TypeError, ValueError):
                attempts = 0
            attempts += 1
            data["submission_reconciliation_attempts"] = attempts
            if attempts < _SUBMISSION_RECONCILIATION_LIMIT:
                vacancy.data = data
                vacancy.state = "SUBMITTING"
                self.emit(
                    db, item.id, "submission_reconciliation",
                    "Ожидается подтверждение отправки отклика",
                    {"vacancy_id": vacancy.id, "status": transport_status, "attempt": attempts},
                )
                return True
            self._record_unconfirmed_submission(
                db, item, vacancy, message=submission.message,
            )
            return False
        if transport_status == "needs_input":
            _record_vacancy_error(
                item, vacancy, "APPLICATION_FORM_UNRESOLVED", submission.message
            )
            return False
        vacancy.state = transport_status.upper()
        existing = db.scalar(select(Application).where(Application.vacancy_id == vacancy.id))
        if existing is None:
            db.add(Application(vacancy_id=vacancy.id,
                               status=submission.status,
                               submitted_at=datetime.now(timezone.utc) if submission.status == "submitted" else None))
            if submission.status in {"submitted", "already_applied"}:
                _increment_counter(db, item, submission.status)
        # The outcome and its counter are one transaction, including crash recovery.
        self.emit(db, item.id, "submission", submission.message,
                  {"vacancy_id": vacancy.id, "status": submission.status})
        return False

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
            if item is None:
                return
            snapshot = db.scalar(
                select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id)
            )
            if snapshot is None:
                raise ValueError("Для сессии не найден временный снимок резюме")
            if snapshot.source_site != item.adapter_id:
                raise ValueError("Временный снимок резюме принадлежит другому сайту")
            resume_content_hash = _snapshot_content_hash(snapshot)
            # The private view is decrypted only in this local process. It
            # supports local form/letter rendering and restores identity
            # and contacts in the complete model payload, including the
            # legacy snapshot fallback.
            private_context = _unseal_private(snapshot.private_view)
            private_identity = private_context.get("identity", {})
            gender = private_identity.get("gender", {}) if isinstance(private_identity, dict) else {}
            gender_value = gender.get("value") if isinstance(gender, dict) else gender
            writer_profile = {"gender": gender_value} if gender_value in {"male", "female"} else {}
            profile = {}
            selected_resumes = [_snapshot_model_payload(snapshot, private_context)]
            search_metrics.initialize(db, item, {}, selected_resumes)
            resume_file = ""
            minimum_scores = item.minimum_scores or None
            stored_policy = item.preference_policy
            preference_description = getattr(item, "desired_job_description", "") or ""
            if private_context:
                # Keep the persisted/UI setting intact, but do not copy
                # literal snapshot identity or contacts into preferences.
                # Those fields are supplied separately by the complete
                # normalized resume payload.
                preference_description = _redact_private_string(
                    preference_description, private_context
                )
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

        # Session-scoped imports use the normalized immutable snapshot payload
        # for every downstream consumer.
        if not selected_resumes:
            raise RecoverableFailure("Для оценки вакансий не выбрано ни одного резюме")

        gateway = ModelGateway()
        if preference_description and stored_policy is None:
            preference_policy = await compile_preference_policy(gateway, preference_description)
            with SessionLocal() as db:
                db.get(JobSession, session_id).preference_policy = preference_policy.model_dump(mode="json")
                db.commit()
        if adapter_id == "hh":
            adapter = RecoveryAdapter(AdaptiveSearch(adapter.adapter, gateway, selected_resumes, preference_policy))
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
        elif adapter_id == "hh":
            portfolio_queries = await plan_portfolio(gateway, selected_resumes, preference_policy)
            search_filters = {"portfolio_queries": portfolio_queries}
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
                        observer = getattr(adapter, "observe_overlap", None)
                        if observer:
                            observer(ref.external_id)
                        search_metrics.record("overlap", {"external_id": ref.external_id})
                        search_metrics.flush(db, session_id)
                        db.commit()
                    continue
            source_posting = None
            posting_was_sanitized = False
            try:
                processing_started = perf_counter()
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
                                    data={"blocker": blocker.kind, "message": blocker.message},
                                )
                                db.add(vacancy)
                                db.flush()
                            _record_blocker_outcome(item, blocker, vacancy)
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
                source_posting = await adapter.extract_job(executor.page)
                # Keep the adapter's original posting for the UI/audit trail,
                # while every evaluator and model path receives a sanitized
                # working copy. IDs and URLs are preserved by the sanitizer.
                posting = sanitize_untrusted_input(source_posting, context="vacancy")
                original_payload = source_posting.model_dump(mode="json")
                working_payload = (
                    posting.model_dump(mode="json")
                    if hasattr(posting, "model_dump") else posting
                )
                posting_was_sanitized = original_payload != working_payload
            except CaptchaRequired:
                raise
            except PromptInjectionDetected as exc:
                with SessionLocal() as db:
                    item = db.get(JobSession, session_id)
                    if not item:
                        continue
                    vacancy = db.scalar(
                        select(Vacancy).where(
                            *_vacancy_scope(adapter_id, adapter.site_id, ref.external_id, session_id)
                        )
                    )
                    # A non-HH scope can find a historical record shared by
                    # sessions; never rewrite that record because of a new
                    # untrusted extraction.
                    if vacancy is not None and vacancy.session_id != session_id:
                        vacancy = None
                    if vacancy is None:
                        vacancy = Vacancy(
                            session_id=session_id,
                            source=adapter.site_id,
                            external_id=ref.external_id,
                            url=ref.url,
                            title="Небезопасная вакансия",
                            data={},
                        )
                        db.add(vacancy)
                        db.flush()
                    _record_security_incident(
                        db, item, vacancy, exc, context="vacancy", emit=self.emit
                    )
                    db.commit()
                continue
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
                    stored_posting = source_posting or posting
                    vacancy = Vacancy(
                        session_id=session_id,
                        source=posting.source,
                        external_id=posting.external_id,
                        url=posting.url,
                        title=stored_posting.title,
                        company=stored_posting.company,
                        state="EXTRACTED",
                        data=stored_posting.model_dump(mode="json"),
                    )
                    db.add(vacancy)
                    db.commit()
                    db.refresh(vacancy)
                    db.add(VacancySnapshot(vacancy_id=vacancy.id, content=stored_posting.description))
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
                if posting_was_sanitized and not (vacancy.data or {}).get("security_ignored_recorded"):
                    vacancy.data = {
                        **(vacancy.data or {}),
                        "security_ignored_recorded": True,
                        "security_ignored": {
                            "count": 1,
                            "reason_code": "instruction_like_text_sanitized",
                        },
                    }
                    self.emit(
                        db,
                        session_id,
                        "security_ignored",
                        "Небезопасная инструкция в данных вакансии проигнорирована",
                        {
                            "vacancy_id": vacancy.id,
                            "kind": "vacancy",
                            "automatic": True,
                            "sanitized": True,
                            "count": 1,
                            "reason_code": "instruction_like_text_sanitized",
                        },
                    )

                if vacancy.state == "SUBMITTING":
                    # Crash recovery must revalidate durable AI state and the
                    # currently visible employer form before reconciliation.
                    recovered_plan_record = db.scalar(
                        select(ApplicationPlanRecord).where(
                            ApplicationPlanRecord.vacancy_id == vacancy.id
                        )
                    )
                    if recovered_plan_record and _cache_matches_resume(
                        recovered_plan_record.data, resume_content_hash
                    ):
                        try:
                            recovered_plan = ApplicationPlan.model_validate(
                                recovered_plan_record.data
                            )
                            _assert_safe_application_plan(
                                recovered_plan, profile, selected_resumes,
                                context="recovered_application_plan",
                            )
                            reader = getattr(adapter, "read_application", None)
                            if reader:
                                current_form = await reader(executor.page)
                                sanitize_untrusted_input(
                                    current_form, context="recovered_application_form"
                                )
                                _assert_safe_application_plan(
                                    recovered_plan,
                                    profile,
                                    selected_resumes,
                                    context="recovered_application_plan_form",
                                    source_form=current_form,
                                )
                        except PromptInjectionDetected as exc:
                            _record_security_incident(
                                db, item, vacancy, exc,
                                context="recovered_application", emit=self.emit,
                            )
                            db.commit()
                            continue
                    verifier = getattr(adapter, "verify_submission", None)
                    if not callable(verifier):
                        self._record_unconfirmed_submission(
                            db, item, vacancy,
                            message="Не удалось подтвердить отправку отклика: адаптер не поддерживает проверку результата",
                        )
                        continue
                    verified = await verifier(executor.page)
                    if verified.status == "already_applied" and (vacancy.data or {}).get("submission_was_absent"):
                        verified = verified.model_copy(update={"status": "submitted"})
                    if verified.status in {"submitted", "already_applied"}:
                        self._record_submission(db, item, vacancy, verified)
                        continue
                    if verified.status == "blocked" and bool(getattr(verified, "confirmed", False)):
                        self._record_submission(db, item, vacancy, verified)
                        continue
                    retry_check = getattr(adapter, "can_retry_application", None)
                    can_retry = bool(retry_check and await retry_check(executor.page))
                    if (vacancy.data or {}).get("submission_attempted") is False and not can_retry:
                        # A form/opening failure is known not to have reached
                        # the submit action.  It is a normal vacancy error,
                        # not an ambiguous transport outcome.
                        _record_vacancy_error(
                            item, vacancy,
                            "VACANCY_PROCESSING_FAILED",
                            "Не удалось открыть или заполнить форму отклика",
                        )
                        db.commit()
                        continue
                    if not can_retry:
                        # No evidence that a second click is safe: retain
                        # SUBMITTING and reconcile on a later bounded pass.
                        if self._record_submission(db, item, vacancy, verified):
                            retry_needed = True
                        continue
                    data = dict(vacancy.data or {})
                    try:
                        attempts = max(0, int(data.get("submission_reconciliation_attempts", 0)))
                    except (TypeError, ValueError):
                        attempts = 0
                    attempts += 1
                    if attempts >= _SUBMISSION_RECONCILIATION_LIMIT:
                        self._record_unconfirmed_submission(
                            db, item, vacancy,
                            message="Не удалось безопасно восстановить отправку отклика после нескольких попыток",
                        )
                        continue
                    data["submission_reconciliation_attempts"] = attempts
                    vacancy.data = data
                    vacancy.state = "READY_TO_SUBMIT"
                    self.emit(
                        db, item.id, "submission_reconciliation",
                        "Сайт подтвердил отсутствие отклика; подготовлено безопасное повторное отправление",
                        {"vacancy_id": vacancy.id, "status": verified.status, "attempt": attempts},
                    )
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
                refresh_cached_evaluation = False
                if evaluation_record and _cache_matches_resume(evaluation_record.data, resume_content_hash):
                    result = JobEvaluation.model_validate(evaluation_record.data)
                    try:
                        # Cached model output is untrusted just like a fresh response.
                        assert_safe_output(result, context="cached_evaluation")
                    except PromptInjectionDetected as exc:
                        _record_security_incident(
                            db, item, vacancy, exc, context="cached_evaluation", emit=self.emit
                        )
                        db.commit()
                        continue
                    # A legacy cached apply result may contain the evaluator's
                    # defaulted all-zero red matches.  It predates the strict
                    # preference contract and must be re-evaluated before any
                    # submission can be prepared.
                    if (
                        result.decision == "apply"
                        and preference_policy
                        and preference_policy.red_flags
                        and not result.preference_flags_verified
                    ):
                        refresh_cached_evaluation = True
                    if (
                        result.decision == "apply"
                        and (vacancy.data or {}).get("evaluation_security_version")
                        != _EVALUATION_SECURITY_VERSION
                    ):
                        # Do not let a pre-guard cached apply authorize an
                        # application. It is re-evaluated once and stamped only
                        # after passing the current evaluator path.
                        refresh_cached_evaluation = True
                elif evaluation_record:
                    # A row without the current immutable snapshot hash is a
                    # legacy/foreign artifact. Keep the row for its unique
                    # vacancy key, but force a fresh model evaluation.
                    refresh_cached_evaluation = True
                if evaluation_record is None or refresh_cached_evaluation:
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
                        assert_safe_output(result, context="evaluation")
                    except PromptInjectionDetected as exc:
                        _record_security_incident(
                            db, item, vacancy, exc, context="evaluation", emit=self.emit
                        )
                        db.commit()
                        continue
                    except ModelUnavailable:
                        raise
                db.refresh(item)
                if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                    return
                if evaluation_record is None:
                    evaluation_data = result.model_dump()
                    if resume_content_hash:
                        evaluation_data[_RESUME_HASH_KEY] = resume_content_hash
                    db.add(Evaluation(vacancy_id=vacancy.id, data=evaluation_data))
                elif refresh_cached_evaluation:
                    evaluation_record.data = {
                        **result.model_dump(),
                        **({_RESUME_HASH_KEY: resume_content_hash} if resume_content_hash else {}),
                    }
                elif resume_content_hash and _RESUME_HASH_KEY not in (evaluation_record.data or {}):
                    # This branch is only reachable for an old result that
                    # was accepted by a legacy caller; never let a snapshot
                    # session persist an unscoped cache marker.
                    evaluation_record.data = {
                        **(evaluation_record.data or {}), _RESUME_HASH_KEY: resume_content_hash
                    }
                if evaluation_record is None or refresh_cached_evaluation:
                    vacancy.data = {
                        **(vacancy.data or {}),
                        "evaluation_security_version": _EVALUATION_SECURITY_VERSION,
                    }
                if evaluation_record is None or refresh_cached_evaluation:
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
                observer = getattr(adapter, "observe", None)
                if observer:
                    await observer(executor.page, posting, result.decision, perf_counter() - processing_started)
                    # Feedback changes the scheduler, not the discovery queue.
                    # Avoid rescanning all vacancies after every evaluation.
                    item.recovery = {**(item.recovery or {}), "search_checkpoint": adapter.search_checkpoint()}
                    search_metrics.flush(db, session_id)
                    db.commit()
                if result.decision == "skip":
                    vacancy.state = "REJECTED_BY_MODEL"
                    counters = dict(item.counters)
                    counters["filtered"] += 1
                    item.counters = counters
                else:
                    if evaluation_record is None and not refresh_cached_evaluation:
                        _increment_counter(db, item, "matched", persist=True)
                    # Persist before awaiting the model. A later refresh would
                    # otherwise discard the dirty JSON counter value.
                    plan = ApplicationPlan(
                        vacancy_id=vacancy.id,
                        resume_file=resume_file,
                        submission_allowed=adapter_id != "hirehi",
                    )
                    plan_record = db.scalar(
                        select(ApplicationPlanRecord).where(
                            ApplicationPlanRecord.vacancy_id == vacancy.id
                        )
                    )
                    if plan_record and _cache_matches_resume(plan_record.data, resume_content_hash):
                        plan = ApplicationPlan.model_validate(plan_record.data)
                        try:
                            _assert_safe_application_plan(
                                plan, profile, selected_resumes, context="cached_application_plan"
                            )
                        except PromptInjectionDetected as exc:
                            _record_security_incident(
                                db, item, vacancy, exc,
                                context="cached_application_plan", emit=self.emit,
                            )
                            db.commit()
                            continue
                    else:
                        plan_data = plan.model_dump()
                        if resume_content_hash:
                            plan_data[_RESUME_HASH_KEY] = resume_content_hash
                        if plan_record is None:
                            plan_record = ApplicationPlanRecord(vacancy_id=vacancy.id, data=plan_data)
                            db.add(plan_record)
                        else:
                            plan_record.data = plan_data
                    cover_record = db.scalar(
                        select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id)
                    )
                    stale_cover_record = None
                    cover_hash = (vacancy.data or {}).get(_COVER_LETTER_HASH_KEY)
                    cover_reusable = (
                        cover_record is not None
                        and (resume_content_hash is None or cover_hash == resume_content_hash)
                    )
                    if cover_record is not None and not cover_reusable:
                        stale_cover_record = cover_record
                        cover_record = None
                    if cover_record and cover_reusable:
                        letter = cover_record.text
                        if private_context:
                            letter = _redact_private_string(letter, private_context)
                            cover_record.text = letter
                        try:
                            assert_safe_outgoing_text(
                                letter, profile, selected_resumes, context="cached_cover_letter"
                            )
                        except PromptInjectionDetected as exc:
                            _record_security_incident(
                                db, item, vacancy, exc,
                                context="cached_cover_letter", emit=self.emit,
                            )
                            db.commit()
                            continue
                        valid, _reason = validate_cover_letter(
                            letter,
                            posting.description,
                            max_words=item.cover_letter_max_words,
                        )
                        if not valid:
                            # A cached letter may have been created with a
                            # different session cap. Keep its row so the
                            # regenerated result replaces it below.
                            stale_cover_record = cover_record
                            cover_record = None
                    if cover_record is None or not cover_reusable:
                        try:
                            letter_kwargs: dict[str, Any] = {
                                "cover_letter_auto": item.cover_letter_auto,
                                "cover_letter_template": item.cover_letter_template,
                            }
                            # Keep the omitted value backwards-compatible for
                            # integrations that wrap the writer, while an
                            # explicit session setting is passed through.
                            if item.cover_letter_max_words is not None:
                                letter_kwargs["cover_letter_max_words"] = item.cover_letter_max_words
                            if private_context:
                                letter_kwargs["private_view"] = private_context
                            letter = await write_cover_letter(
                                posting,
                                writer_profile,
                                selected_resumes,
                                gateway,
                                preference_policy,
                                **letter_kwargs,
                            )
                            if private_context:
                                letter = _redact_private_string(letter, private_context)
                            assert_safe_outgoing_text(
                                letter, profile, selected_resumes, context="cover_letter"
                            )
                        except PromptInjectionDetected as exc:
                            _record_security_incident(
                                db, item, vacancy, exc, context="cover_letter", emit=self.emit
                            )
                            db.commit()
                            continue
                        except CoverLetterValidationError as exc:
                            data = dict(vacancy.data or {})
                            try:
                                previous_attempts = int(data.get("cover_letter_attempts", 0))
                            except (TypeError, ValueError):
                                previous_attempts = 0
                            attempts = max(0, previous_attempts) + 1
                            data["cover_letter_attempts"] = attempts
                            data["cover_letter_error"] = str(exc)
                            vacancy.data = data
                            if attempts < _COVER_LETTER_RETRY_LIMIT:
                                vacancy.state = "EVALUATING"
                                retry_needed = True
                                self.emit(
                                    db,
                                    session_id,
                                    "vacancy_retry",
                                    "Сопроводительное письмо будет сгенерировано повторно",
                                    {
                                        "kind": "cover_letter",
                                        "vacancy_id": vacancy.id,
                                        "attempt": attempts,
                                        "error": str(exc),
                                    },
                                )
                            else:
                                _record_vacancy_error(
                                    item,
                                    vacancy,
                                    "VACANCY_PROCESSING_FAILED",
                                    "Не удалось автоматически подготовить сопроводительное письмо",
                                )
                                self.emit(
                                    db,
                                    session_id,
                                    "vacancy_error",
                                    "Вакансия не обработана: сопроводительное письмо не удалось подготовить автоматически",
                                    {
                                        "kind": "cover_letter",
                                        "vacancy_id": vacancy.id,
                                        "attempts": attempts,
                                        "automatic": True,
                                        "reason_code": "VACANCY_PROCESSING_FAILED",
                                    },
                                )
                            continue
                        except ModelUnavailable:
                            raise
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                        return
                    plan.cover_letter = letter
                    if vacancy.data and "cover_letter_attempts" in vacancy.data:
                        data = dict(vacancy.data)
                        data.pop("cover_letter_attempts", None)
                        data.pop("cover_letter_error", None)
                        vacancy.data = data
                    plan.allow_foreign_application = adapter_id == "hh"
                    plan_data = _redact_plan(plan, private_context) if private_context else plan.model_dump()
                    if resume_content_hash:
                        plan_data[_RESUME_HASH_KEY] = resume_content_hash
                    plan_record.data = plan_data
                    if cover_record is None and stale_cover_record is None:
                        db.add(CoverLetter(vacancy_id=vacancy.id, text=letter))
                    elif stale_cover_record is not None:
                        stale_cover_record.text = letter
                    if resume_content_hash:
                        vacancy.data = {
                            **(vacancy.data or {}), _COVER_LETTER_HASH_KEY: resume_content_hash
                        }
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
                        _record_blocker_outcome(item, blocker, vacancy)
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
                            sanitize_untrusted_input(route, context="application_route")
                            form = None
                        else:
                            retry_check = getattr(adapter, "can_retry_application", None)
                            absent = bool(retry_check and await retry_check(executor.page))
                            vacancy.data = {
                                **(vacancy.data or {}),
                                "submission_was_absent": absent,
                                # Opening/filling a form is not a submission;
                                # this flips only immediately before the
                                # adapter's actual submit operation.
                                "submission_attempted": False,
                            }
                            # Opening HH's form can itself send a one-click application.
                            vacancy.state = "SUBMITTING"
                            db.commit()
                            form = await adapter.open_application(executor.page)
                            # Keep the live form for adapter binding, while the
                            # model sees a sanitized copy with the same IDs and
                            # options.
                            model_form = sanitize_untrusted_input(form, context="application_form")
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
                            if plan.cover_letter:
                                assert_safe_outgoing_text(
                                    plan.cover_letter,
                                    profile,
                                    selected_resumes,
                                    context="hirehi_report_letter",
                                )
                            try:
                                summary_payload = {"job": {"title": vacancy.title, "description": getattr(posting, "description", "")}}
                                if preference_policy:
                                    summary_payload["preference_policy"] = preference_policy.model_dump(mode="json")
                                summary = await gateway.structured("job_summary", summary_payload, JobSummary)
                                assert_safe_output(summary, context="job_summary")
                                short_description = summary.summary
                            except PromptInjectionDetected as exc:
                                _record_security_incident(
                                    db, item, vacancy, exc, context="job_summary", emit=self.emit
                                )
                                db.commit()
                                continue
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
                            _record_vacancy_error(
                                item,
                                vacancy,
                                "UNKNOWN_APPLICATION_ROUTE",
                                "Не удалось определить маршрут отклика на вакансии",
                            )
                            self.emit(
                                db,
                                session_id,
                                "vacancy_error",
                                "Не удалось определить маршрут отклика",
                                {"vacancy_id": vacancy.id},
                            )
                            db.commit()
                            continue
                        if adapter_id == "hh":
                            def checkpoint(current_plan, item=item, plan_record=plan_record):
                                db.refresh(item)
                                if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                                    return False
                                plan_data = (
                                    _redact_plan(current_plan, private_context)
                                    if private_context else current_plan.model_dump()
                                )
                                if resume_content_hash:
                                    plan_data[_RESUME_HASH_KEY] = resume_content_hash
                                plan_record.data = plan_data
                                db.commit()
                                return True

                            vacancy.data = {
                                **(vacancy.data or {}), "submission_attempted": True,
                            }
                            db.commit()
                            outcome = await complete_application(
                                adapter, executor.page, plan, posting, profile, selected_resumes,
                                preference_description, gateway, checkpoint,
                                guaranteed_application=item.guaranteed_application,
                                private_view=private_context or None,
                            )
                            if outcome.stopped:
                                return
                            if outcome.error_code:
                                if outcome.unanswered_questions:
                                    vacancy.data = {
                                        **(vacancy.data or {}),
                                        "application_error_reasons": outcome.unanswered_questions,
                                        "application_unanswered_questions": outcome.unanswered_questions,
                                    }
                                _record_vacancy_error(
                                    item,
                                    vacancy,
                                    outcome.error_code,
                                    outcome.error_message or "Не удалось обработать форму отклика",
                                )
                                self.emit(
                                    db,
                                    session_id,
                                    "vacancy_error",
                                    "Вакансия не обработана: форму не удалось безопасно заполнить автоматически",
                                    {
                                        "vacancy_id": vacancy.id,
                                        "kind": "application_questions",
                                        "reason_count": len(outcome.unanswered_questions),
                                        "automatic": True,
                                        "reason_code": outcome.error_code,
                                    },
                                )
                                db.commit()
                                continue
                            submission = outcome.submission
                            if self._record_submission(db, item, vacancy, submission):
                                retry_needed = True
                            db.commit()
                            continue
                        from backend.intelligence.application_answers import prepare_answers

                        plan = await prepare_answers(
                            gateway, form, plan, posting, profile, selected_resumes, preference_description,
                            guaranteed_application=item.guaranteed_application,
                            private_view=private_context or None,
                        )
                        if private_context:
                            plan.cover_letter = render_local_private(plan.cover_letter, private_context) if plan.cover_letter else ""
                            for answer in plan.form_answers.values():
                                answer.values = [render_local_private(value, private_context) for value in answer.values]
                            plan.known_answers = {
                                key: render_local_private(value, private_context)
                                for key, value in plan.known_answers.items()
                            }
                        _assert_safe_application_plan(
                            plan,
                            profile,
                            [*selected_resumes, private_context] if private_context else selected_resumes,
                            context="application_plan",
                            source_form=form,
                        )
                        plan_data = _redact_plan(plan, private_context) if private_context else plan.model_dump()
                        if resume_content_hash:
                            plan_data[_RESUME_HASH_KEY] = resume_content_hash
                        plan_record.data = plan_data
                        db.commit()
                        db.refresh(item)
                        if item.status in {SessionStatus.STOPPED, SessionStatus.PAUSED}:
                            return
                        result = await adapter.fill_application(executor.page, plan)
                        model_result = sanitize_untrusted_input(result, context="application_fill_result")
                    except PromptInjectionDetected as exc:
                        _record_security_incident(
                            db, item, vacancy, exc, context="application_flow", emit=self.emit
                        )
                        db.commit()
                        continue
                    except (ModelUnavailable, CaptchaRequired):
                        raise
                    except Exception:
                        # Keep SUBMITTING durable; reconcile it on the next attempt.
                        if adapter_id == "hirehi":
                            retry_needed = True
                            continue
                        raise
                    questions = unresolved_application_questions(model_form, model_result)
                    if questions:
                        vacancy.data = {
                            **(vacancy.data or {}),
                            "application_error_reasons": questions,
                            "application_unanswered_questions": questions,
                        }
                        _record_vacancy_error(
                            item,
                            vacancy,
                            "APPLICATION_FORM_UNRESOLVED",
                            "Не удалось подтвердить заполнение обязательных вопросов анкеты",
                        )
                        self.emit(
                            db,
                            session_id,
                            "vacancy_error",
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
                            reader = getattr(adapter, "read_application", None)
                            current_form = form
                            if reader:
                                current_form = await reader(executor.page)
                                sanitize_untrusted_input(
                                    current_form, context="application_form_before_submit"
                                )
                            _assert_safe_application_plan(
                                plan,
                                profile,
                                [*selected_resumes, private_context] if private_context else selected_resumes,
                                context="application_plan_before_submit",
                                source_form=current_form,
                            )
                            vacancy.data = {
                                **(vacancy.data or {}), "submission_attempted": True,
                            }
                            db.commit()
                            submission = await adapter.submit_application(executor.page)
                        except PromptInjectionDetected as exc:
                            _record_security_incident(
                                db, item, vacancy, exc,
                                context="application_before_submit", emit=self.emit,
                            )
                            db.commit()
                            continue
                        except CaptchaRequired:
                            raise
                        except Exception:
                            raise
                        if self._record_submission(db, item, vacancy, submission):
                            retry_needed = True
                db.commit()
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if not retry_needed:
                    # A clean pass proves durable progress (all queued work
                    # reached a terminal state).  Pending retries and failed
                    # extraction/model stages retain their budget so repeated
                    # no-progress passes eventually become FAILED.
                    item.recovery = {
                        **(item.recovery or {}),
                        "attempt": 0,
                        "retry_at": None,
                        "message": None,
                    }
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
            JobSession.status.in_((SessionStatus.RUNNING,))
        )))
    for session_id in recovered:
        workflow_manager.launch(session_id)
    return recovered
