from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Any

from sqlalchemy import select

from backend.adapters import adapter_registry
from backend.adapters.base.protocol import JobRef
from backend.browser.sessions import close_browser, get_browser, restore_browser
from backend.config import settings
from backend.intelligence.adaptive_search_planner import plan_portfolio
from backend.intelligence.broker_gateway import BrokeredModelGateway
from backend.intelligence.evaluator import _payload, evaluate
from backend.intelligence.gateway import (
    ModelGateway,
    ModelPermanentError,
    ModelTimeout,
    ModelUnavailable,
)
from backend.intelligence.hirehi_category import JobSummary, choose_hirehi_category
from backend.intelligence.hirehi_grade import hirehi_grades
from backend.intelligence.letter_writer import (
    CoverLetterValidationError,
    validate_cover_letter,
    write_cover_letter,
)
from backend.intelligence.model_broker import ModelRequestClient
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
from backend.orchestrator.hirehi_adaptive_search import HireHiAdaptiveSearch
from backend.orchestrator.pipeline import (
    EVALUATION_QUEUE_LIMIT,
    DurablePipelineCoordinator,
    PipelineStore,
    current_generation,
)
from backend.orchestrator.recovery import (
    AuthenticationPending,
    CaptchaRequired,
    RecoverableFailure,
    RecoveryAdapter,
)
from backend.orchestrator.search_version import (
    HIREHI_SEARCH_ADAPTIVE_V3,
    HIREHI_SEARCH_V1,
    is_hirehi_adaptive,
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
from backend.runtime.lifecycle import cancellation_fence
from backend.schemas import domain as domain_schemas
from backend.schemas.domain import (
    ApplicationPlan,
    DesiredJobPolicy,
    JobEvaluation,
    SessionStatus,
)
from backend.services import search_metrics
from backend.services.hirehi_reporting import write_session_pdf
from backend.services.private_text import _messenger_urls, _unseal_private, render_local_private
from backend.services.resume_session import (
    _sha256,
    full_resume_model_payload,
    professional_view,
)

_INJECTABLE_DIRECT_GATEWAY = ModelGateway


def _increment_counter(db, item: JobSession, key: str, *, persist: bool = False) -> None:
    counters = dict(item.counters)
    counters[key] = counters.get(key, 0) + 1
    item.counters = counters
    if persist:
        db.commit()


def _limit_reached(count: int, limit: int | None) -> bool:
    """Unlimited session limits are represented by ``None``."""
    return limit is not None and count >= limit


def _configured_hirehi_version() -> str:
    value = getattr(settings, "hirehi_search_version", HIREHI_SEARCH_ADAPTIVE_V3)
    return value if value in {HIREHI_SEARCH_V1, HIREHI_SEARCH_ADAPTIVE_V3} else HIREHI_SEARCH_ADAPTIVE_V3


def _hirehi_version(item: JobSession | None = None) -> str:
    if item is not None:
        persisted = (item.recovery or {}).get("search_version")
        if persisted in {HIREHI_SEARCH_V1, HIREHI_SEARCH_ADAPTIVE_V3}:
            return persisted
    return _configured_hirehi_version()


def _initial_hirehi_version(item: JobSession) -> str:
    """Resolve an unversioned row once, retaining known legacy identities."""
    identity = (item.recovery or {}).get("measurement_identity") or {}
    historical = identity.get("algorithm_version", identity.get("algorithm"))
    if historical in {HIREHI_SEARCH_V1, "existing_v1"}:
        return HIREHI_SEARCH_V1
    return _configured_hirehi_version()


def _hirehi_adaptive(item: JobSession | None, adapter: Any | None = None) -> bool:
    if not item or item.adapter_id != "hirehi" or not is_hirehi_adaptive(_hirehi_version(item)):
        return False
    return adapter is None or all(hasattr(adapter, name) for name in ("open_source", "collect_card_refs"))


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _guarded_representational_call(
    db: Any,
    session_id: int,
    generation: int | None,
    operation: Callable[[], Any],
) -> tuple[bool, Any]:
    """Fence cancellation immediately before a browser-side operation.

    Keeping the check and await in one helper prevents a future caller from
    accidentally inserting a database/model await between the fence and an
    operation that can represent or send an application.
    """
    if cancellation_fence(db, session_id, generation):
        return False, None
    return True, await _await_if_needed(operation())


def _normalized_recovery_counters(counters: Any) -> dict[str, int | float]:
    """Return only stable numeric counters used to detect real workflow progress."""
    if not isinstance(counters, dict):
        return {}
    normalized: dict[str, int | float] = {}
    for key, value in counters.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            normalized[str(key)] = value
            continue
        if isinstance(value, float):
            if math.isfinite(value):
                normalized[str(key)] = value
            continue
        # Counters are persisted as JSON, but tolerate numeric strings from
        # older rows without allowing arbitrary recovery metadata through.
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                normalized[str(key)] = int(number) if number.is_integer() else number
    return dict(sorted(normalized.items()))


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
    "SUBMISSION_UNCONFIRMED",
    "SESSION_STOPPED",
    "SESSION_FAILED",
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


def _hirehi_snapshot_data(engine: Any) -> dict[str, Any]:
    """Build privacy-safe adaptive telemetry without vacancy text or IDs."""
    raw_metrics = engine.metrics() if hasattr(engine, "metrics") else {}
    metrics = {key: value for key, value in raw_metrics.items() if key != "audit"}
    stats = {}
    for key, source in getattr(getattr(engine, "scheduler", None), "sources", {}).items():
        stats[str(key)] = {
            "family": str(getattr(source, "family", "")),
            "raw": int(getattr(source, "raw_discovered", 0)),
            "unique": int(getattr(source, "unique_discovered", 0)),
            "analyzed": int(getattr(source, "analyzed", 0)),
            "relevant": int(getattr(source, "relevant", 0)),
            "failures": int(getattr(source, "failures", 0)),
            "exhausted": bool(getattr(source, "exhausted", False)),
        }
    audit = getattr(engine, "audit_metrics", lambda: {})()
    return {
        "sources": stats,
        "metrics": metrics,
        "audit": {key: value for key, value in audit.items()
                  if key not in {"eligible_ids", "selected_ids"}},
        "rejection_reasons": dict(getattr(engine, "rejection_reasons", {}) or {}),
    }


def _apply_hirehi_weak_prior(engine: Any, prior: Any) -> None:
    """Apply a tiny prior to scheduler arms; it never changes D/N/R labels."""
    if not isinstance(prior, dict):
        return
    importer = getattr(engine, "import_weak_priors", None)
    if callable(importer):
        checkpoint = engine.search_checkpoint()
        importer({
            "algorithm_version": checkpoint.get("algorithm_version"),
            "criteria_hash": checkpoint.get("criteria_hash"),
            "sources": prior,
        })
        return
    sources = getattr(getattr(engine, "scheduler", None), "sources", {})
    for key, source in sources.items():
        candidates = [str(key), str(getattr(source, "spec", {}).get("source_id", "")),
                      str(getattr(source, "spec", {}).get("query", ""))]
        row = next((prior[name] for name in candidates if name and name in prior), None)
        if not isinstance(row, dict):
            continue
        # One pseudo-observation is enough to break ties while preserving
        # exploration and keeping historical data bounded by the service.
        source.prior_successes = min(1.0, max(0.0, float(row.get("successes", 0) or 0)))
        source.prior_failures = min(1.0, max(0.0, float(row.get("failures", 0) or 0)))


_HIREHI_REASON_MARKERS = {
    "wrong_role": ("role_match", "role", "должност", "роль"),
    "wrong_grade": ("grade", "level", "seniority", "грейд", "уров"),
    "hard_skill_missing": ("skill", "skills", "навык", "технолог"),
    "excluded_domain": ("excluded_domain", "исключ", "запрещ"),
    "location": ("location", "местополож", "локац", "город"),
    "work_format": ("work_format", "формат", "удалён", "удален", "офис"),
    "language": ("language", "язык"),
    "salary": ("salary", "зарплат", "доход"),
    "insufficient_evidence": ("insufficient", "evidence", "доказательств", "данных недостат"),
}


def _hirehi_evaluation_reason(result: JobEvaluation) -> str:
    """Map only known evaluator fields to a bounded scheduler reason enum."""
    values = [*(result.minimum_score_violations or []), *(result.hard_rule_violations or [])]
    normalized = [str(value).casefold()[:120] for value in values]
    for reason, markers in _HIREHI_REASON_MARKERS.items():
        if any(any(marker in value for marker in markers) for value in normalized):
            return reason
    if result.decision == "skip" and result.confidence < 0.5:
        return "insufficient_evidence"
    return "other"


_COVER_LETTER_RETRY_LIMIT = 3
_SUBMISSION_RECONCILIATION_LIMIT = 3
_SESSION_RECOVERY_RETRY_LIMIT = 8
_MODEL_STAGE_RETRY_LIMIT = 8
_RECOVERY_COUNTERS_KEY = "progress_counters"
_EVALUATION_SECURITY_VERSION = 1
_RESUME_HASH_KEY = "_resume_content_hash"
_COVER_LETTER_HASH_KEY = "cover_letter_content_hash"
_PLAN_URL_RE = re.compile(r"https?://[^\s<>\]\[(){}]+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = '.,;:!?\"\''


def _stage_retry_attempt(vacancy: Vacancy, stage: str) -> int:
    data = dict(vacancy.data or {})
    budgets = dict(data.get("model_retry_budgets", {}) or {})
    try:
        attempts = max(0, int(budgets.get(stage, 0))) + 1
    except (TypeError, ValueError):
        attempts = 1
    budgets[stage] = attempts
    data["model_retry_budgets"] = budgets
    vacancy.data = data
    return attempts


def _clear_stage_retry_attempt(vacancy: Vacancy, stage: str) -> None:
    data = dict(vacancy.data or {})
    budgets = dict(data.get("model_retry_budgets", {}) or {})
    budgets.pop(stage, None)
    if budgets:
        data["model_retry_budgets"] = budgets
    else:
        data.pop("model_retry_budgets", None)
    vacancy.data = data
def _cache_matches_resume(data: Any, content_hash: str | None) -> bool:
    """Only session-snapshot artifacts may be reused by a snapshot session."""
    if not isinstance(data, dict):
        return content_hash is None
    cached_hash = data.get(_RESUME_HASH_KEY)
    return cached_hash == content_hash if content_hash else cached_hash is None


def _private_mapping(private: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(private, str):
        private = _unseal_private(private)

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
                elif normalized in {"phone", "email"}:
                    walk(child, normalized)
                elif normalized == "messengers":
                    # Messenger URLs get independent placeholders below so a
                    # later phone pass cannot corrupt a URL containing digits.
                    continue
                elif normalized in {"identity", "contacts"}:
                    walk(child)
        elif isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if items and key and key != "messengers":
                result[key] = ", ".join(items)
        elif value is not None and key and key != "messengers" and str(value).strip():
            result[key] = str(value).strip()

    walk(private)
    result.update({f"messenger_{index}": url for index, url in enumerate(_messenger_urls(private))})
    return result


def _redact_private_string(value: str, private: Any) -> str:
    original = str(value or "")
    mapping = _private_mapping(private)
    messenger_mapping = {key: literal for key, literal in mapping.items() if key.startswith("messenger_")}
    protected: list[str] = []

    def protect_urls(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        candidate = raw_url.rstrip(_URL_TRAILING_PUNCTUATION)
        trusted_match = next(
            (
                (key, literal)
                for key, literal in messenger_mapping.items()
                if raw_url == literal or candidate == literal.rstrip(_URL_TRAILING_PUNCTUATION)
            ),
            None,
        )
        if trusted_match is not None:
            key, literal = trusted_match
            placeholder = "{{" + key + "}}"
            suffix = (
                raw_url[len(literal):]
                if raw_url.startswith(literal)
                else raw_url[len(candidate):]
            )
            protected.append(placeholder + suffix)
        else:
            protected.append(raw_url)
        return f"\ue000URL{len(protected) - 1}\ue001"

    # Mask every URL span first. The same token boundaries and terminal
    # punctuation rule as outbound validation keep phone/name matching from
    # changing digits, email-like query values, or any part of an untrusted URL.
    result = _PLAN_URL_RE.sub(protect_urls, original)
    for key, literal in sorted(
        ((key, literal) for key, literal in mapping.items() if key not in messenger_mapping),
        key=lambda pair: len(pair[1]),
        reverse=True,
    ):
        if len(literal) >= 2:
            placeholder = "full_name" if key in {"name", "fio"} else key
            result = re.sub(re.escape(literal), "{{" + placeholder + "}}", result, flags=re.IGNORECASE)
    result = re.sub(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])", "{{email}}", result)
    result = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "{{phone}}", result)
    return re.sub(r"\ue000URL(\d+)\ue001", lambda match: protected[int(match.group(1))], result)


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


def _render_private_plan(plan: ApplicationPlan, private: Any) -> ApplicationPlan:
    """Restore only locally sealed private placeholders before safety checks."""
    trusted_messenger_markers = {
        f"{{{{messenger_{index}}}}}" for index, _ in enumerate(_messenger_urls(private))
    }

    def render(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: render(child) for key, child in value.items()}
        if isinstance(value, list):
            return [render(child) for child in value]
        if isinstance(value, str):
            for match in _PLAN_URL_RE.finditer(value):
                if re.match(
                    r"(?:[.,;:!?\"']\s*)?(?:\{\{|\{%|<%|\$\{)",
                    value[match.end():],
                ):
                    raise PromptInjectionDetected("untrusted_outbound_url", context="cached_application_plan")
            if re.search(
                r"\{\{\s*messenger_\d+\s*\}\}['\".,;:!?]?\s*(?:\{\{|\{%|<%|\$\{)",
                value,
            ):
                raise PromptInjectionDetected("untrusted_outbound_url", context="cached_application_plan")
            for marker in re.findall(r"\{\{\s*messenger_\d+\s*\}\}", value):
                if marker not in trusted_messenger_markers:
                    raise PromptInjectionDetected("untrusted_outbound_url", context="cached_application_plan")
            return render_local_private(value, private)
        return value

    return ApplicationPlan.model_validate(render(plan.model_dump(mode="json")))


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

    def __init__(self, *, generation: int | None = None) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.generation = generation
        self.site_leases: dict[str, int] = {}
        self.task_sites: dict[int, str] = {}
        self._model_prefetch_tasks: dict[int, set[asyncio.Task]] = {}
        # launch() is called synchronously by the API before the async task is
        # created. Serialize the check and in-memory lease claim.
        self._launch_lock = Lock()

    def _pipeline(self) -> PipelineStore:
        # Resolve the factory at call time: isolated workflow tests replace
        # ``SessionLocal`` with their own real SQLite sessionmaker.
        return DurablePipelineCoordinator(SessionLocal)

    def _model_gateway(self, session_id: int, site_id: str):
        # Existing unit/e2e tests deliberately inject a fake gateway at this
        # module boundary. Production workers, however, must never construct
        # a provider-bearing ModelGateway.
        if ModelGateway is not _INJECTABLE_DIRECT_GATEWAY:
            return ModelGateway()
        with SessionLocal() as db:
            generation = current_generation(db, session_id, self.generation)
        return BrokeredModelGateway(
            session_id,
            site_id,
            generation,
            SessionLocal,
            client=ModelRequestClient(SessionLocal),
        )

    def _advance_pipeline(
        self,
        session_id: int,
        site_id: str,
        external_id: str,
        stage: str,
        *,
        vacancy_id: int | None = None,
    ) -> bool:
        return self._pipeline().advance(
            session_id,
            site_id,
            external_id,
            stage,
            generation=self.generation,
            vacancy_id=vacancy_id,
        )

    def _complete_duplicate_pipeline_item(
        self, session_id: int, site_id: str, external_id: str, vacancy_id: int
    ) -> bool:
        """Close this session's queue item without rewriting its duplicate vacancy."""
        return self._advance_pipeline(
            session_id,
            site_id,
            external_id,
            "completed",
            vacancy_id=vacancy_id,
        )

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
        # Keep report finalization bounded to one vacancy/evaluation query.
        # The previous per-row Evaluation lookup became visible on large
        # HireHi sessions and delayed lifecycle transitions unnecessarily.
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
                # Render only the in-memory report row immediately before PDF
                # generation. The placeholder-bearing vacancy JSON remains
                # safe and is never overwritten with the rendered copy.
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
        self.emit(db, session_id, "report_ready", "PDF отчёт сформирован", {
            "path": f"/api/sessions/{session_id}/report/pdf", "count": len(report_rows)
        })
        return len(report_rows)

    def write_hirehi_report(self, session_id: int) -> int:
        """Idempotently generate a HireHi report for completed or stopped sessions."""
        with SessionLocal() as db:
            return self._write_hirehi_report(db, session_id)

    def _terminalize_pending_vacancies(self, db, item: JobSession) -> int:
        """Close unfinished vacancy work when its owning session is terminal."""
        codes = {
            SessionStatus.STOPPED: (
                "SESSION_STOPPED", "Вакансия не обработана: сессия остановлена пользователем"
            ),
            SessionStatus.CANCELLED: (
                "SESSION_CANCELLED", "Вакансия не обработана: сессия отменена пользователем"
            ),
            SessionStatus.FAILED: (
                "SESSION_FAILED", "Вакансия не обработана: сессия завершилась с ошибкой"
            ),
            SessionStatus.COMPLETED: (
                "VACANCY_PROCESSING_FAILED", "Вакансия не обработана до завершения сессии"
            ),
        }
        outcome = codes.get(item.status)
        if outcome is None and item.status != SessionStatus.CANCELLED:
            return 0
        pending_states = {
            "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING",
        }
        vacancies = list(db.scalars(select(Vacancy).where(Vacancy.session_id == item.id)))
        terminalized = 0
        for vacancy in vacancies:
            if vacancy.state not in pending_states:
                continue
            if item.status == SessionStatus.CANCELLED:
                data = dict(vacancy.data or {})
                data["cancellation_code"] = "SESSION_CANCELLED"
                data["cancellation_message"] = "Вакансия не обработана: сессия отменена пользователем"
                vacancy.data = data
                vacancy.state = "CANCELLED"
                terminalized += 1
                continue
            error_code, error_message = outcome
            _record_vacancy_error(item, vacancy, error_code, error_message)
            terminalized += 1
        return terminalized

    def finalize(self, session_id: int, completion_reason: str) -> None:
        """Finalize only active sessions; preserve externally terminal states."""
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item or item.status in {SessionStatus.FAILED, "CANCELLED"}:
                return
            was_stopped = item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED}
            if not was_stopped:
                item.status = SessionStatus.COMPLETED
                item.stop_reason = completion_reason
            item.finished_at = datetime.now(timezone.utc)
            from backend.orchestrator.terminal_finalization import finalize_terminal_session

            finalize_terminal_session(
                db, item, report_writer=self._write_hirehi_report
            )
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
                        if item and item.status not in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.COMPLETED}:
                            item.status = SessionStatus.PAUSED
                            item.stop_reason = str(exc)
                            self.emit(db, session_id, "human_required", str(exc), {"kind": "captcha"})
                    break
                except Exception as exc:
                    await self._cancel_model_prefetch_tasks(session_id)
                    with search_metrics.measure("recovery"):
                        recovered = await self._recover(session_id, exc)
                    if not recovered:
                        break
        finally:
            await self._cancel_model_prefetch_tasks(session_id)
            with SessionLocal() as db:
                if search_metrics.flush(db, session_id):
                    db.commit()
                final_item = db.get(JobSession, session_id)
                final_status = final_item.status if final_item else SessionStatus.FAILED
                if final_item and final_status in {SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.FAILED}:
                    from backend.orchestrator.terminal_finalization import finalize_terminal_session

                    finalize_terminal_session(db, final_item)
            search_metrics.end(metric_token)
            if final_status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.FAILED}:
                request_ids = self._pipeline().cancel_session(
                    session_id, generation=self.generation
                )
                broker_client = ModelRequestClient(SessionLocal)
                for request_id in request_ids:
                    with suppress(Exception):
                        broker_client.cancel(request_id)
            if final_status in {SessionStatus.COMPLETED, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.FAILED}:
                with suppress(Exception):
                    await asyncio.wait_for(close_browser(session_id), timeout=15)
            self.tasks.pop(session_id, None)
            site_id = self.task_sites.pop(session_id, None)
            if site_id and self.site_leases.get(site_id) == session_id:
                self.site_leases.pop(site_id, None)

    async def _cancel_model_prefetch_tasks(self, session_id: int) -> None:
        tasks = tuple(self._model_prefetch_tasks.pop(session_id, set()))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _recover(self, session_id: int, exc: Exception) -> bool:
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item or item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.COMPLETED, SessionStatus.PAUSED}:
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
            dependency_wait = isinstance(exc, (ModelUnavailable, AuthenticationPending))
            current_counters = _normalized_recovery_counters(item.counters)
            previous_counters = recovery.get(_RECOVERY_COUNTERS_KEY)
            has_progress_snapshot = isinstance(previous_counters, dict)
            progress_detected = (
                has_progress_snapshot
                and _normalized_recovery_counters(previous_counters) != current_counters
            )
            if not dependency_wait and progress_detected:
                # A real workflow counter changed since the last recovery;
                # this is forward progress and starts a fresh failure budget.
                previous_attempt = 0
            if not dependency_wait and previous_attempt >= _SESSION_RECOVERY_RETRY_LIMIT:
                item.status = SessionStatus.FAILED
                item.stop_reason = "Сессия остановлена после исчерпания повторов временной ошибки"
                item.finished_at = datetime.now(timezone.utc)
                self.emit(
                    db, session_id, "session_failed", item.stop_reason,
                    {"kind": "recovery_exhausted", "attempts": previous_attempt},
                )
                db.commit()
                return False
            # Model outages can last longer than a browser timeout. Persist an
            # unbounded attempt counter and back off exponentially while the
            # short-slice wait below keeps Stop responsive. Authentication
            # waits remain at a steady cadence so a restored login is noticed.
            attempt = previous_attempt + 1 if isinstance(exc, ModelUnavailable) else (
                previous_attempt if dependency_wait else previous_attempt + 1
            )
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * 2 ** min(max(attempt - 1, 0), 10),
            ) if isinstance(exc, ModelUnavailable) else (
                self.retry_base_seconds if dependency_wait else
                min(self.retry_max_seconds, self.retry_base_seconds * 2 ** min(attempt - 1, 10))
            )
            if isinstance(exc, AuthenticationPending):
                reason = "Ожидаем вход в открытом браузере; проверка продолжится автоматически"
            elif isinstance(exc, ModelUnavailable):
                reason = "Модель временно недоступна"
            else:
                reason = "Временный сбой обработки или загрузки страницы"
            message = f"{reason}. Автоматический повтор через {delay} с."
            recovery.update(
                attempt=attempt,
                retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
                message=message,
                **{_RECOVERY_COUNTERS_KEY: current_counters},
            )
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
            return status not in {
                SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED,
                SessionStatus.FAILED, SessionStatus.COMPLETED,
            }

    async def _model_stage_call(
        self, db, item: JobSession, vacancy: Vacancy, stage: str, operation: Callable[[], Any]
    ) -> tuple[bool, Any]:
        """Run a model stage once; persist transient failures for queue deferral."""
        data = dict(vacancy.data or {})
        if data.get("model_retry_stage") == stage and data.get("model_retry_at"):
            try:
                retry_at = datetime.fromisoformat(data["model_retry_at"])
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                retry_at = None
            if retry_at is not None and retry_at > datetime.now(timezone.utc):
                return False, None
        started = perf_counter()
        try:
            result = await _await_if_needed(operation())
        except ModelPermanentError:
            search_metrics.record("model", {
                "stage": stage, "seconds": perf_counter() - started,
                "outcome": "permanent_error",
            })
            raise
        except ModelTimeout:
            search_metrics.record("model", {
                "stage": stage, "seconds": perf_counter() - started,
                "outcome": "logical_timeout",
            })
            _stage_retry_attempt(vacancy, stage)
            _record_vacancy_error(
                item, vacancy, "VACANCY_PROCESSING_FAILED",
                f"Истекло время модельного запроса на этапе {stage}",
            )
            self.emit(
                db, item.id, "vacancy_error",
                "Вакансия пропущена: модельный запрос превысил допустимое время",
                {"vacancy_id": vacancy.id, "stage": stage, "error_type": "ModelTimeout"},
            )
            db.commit()
            return False, None
        except CoverLetterValidationError:
            # Validation failures are handled by the vacancy-local bounded
            # cover-letter retry path below. They are not provider outages.
            raise
        except ModelUnavailable as exc:
            search_metrics.record("model", {
                "stage": stage, "seconds": perf_counter() - started,
                "outcome": "unavailable",
            })
            attempts = _stage_retry_attempt(vacancy, stage)
            if attempts >= _MODEL_STAGE_RETRY_LIMIT:
                _record_vacancy_error(
                    item, vacancy, "VACANCY_PROCESSING_FAILED",
                    f"Исчерпаны повторы временной ошибки этапа {stage}",
                )
                self.emit(
                    db, item.id, "vacancy_error",
                    "Вакансия пропущена: исчерпаны повторы временной ошибки модели",
                    {"vacancy_id": vacancy.id, "stage": stage, "attempts": attempts},
                )
                db.commit()
                return False, None
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * 2 ** min(attempts - 1, 10),
            )
            data = dict(vacancy.data or {})
            data["model_retry_stage"] = stage
            data["model_retry_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
            vacancy.data = data
            self.emit(
                db, item.id, "model_stage_retry",
                "Модель временно недоступна; вакансия отложена до следующей попытки",
                {"vacancy_id": vacancy.id, "stage": stage, "attempt": attempts,
                 "delay_seconds": delay, "error_type": type(exc).__name__},
            )
            db.commit()
            return False, None
        else:
            search_metrics.record("model", {
                "stage": stage, "seconds": perf_counter() - started,
                "outcome": "ok",
            })
            data = dict(vacancy.data or {})
            data.pop("model_retry_stage", None)
            data.pop("model_retry_at", None)
            vacancy.data = data
            _clear_stage_retry_attempt(vacancy, stage)
            return True, result

    async def _wait_for_model_retry(self, session_id: int) -> bool:
        """Wait for the earliest deferred model stage without reopening search."""
        with SessionLocal() as db:
            rows = list(db.scalars(select(Vacancy).where(
                Vacancy.session_id == session_id,
                Vacancy.state.in_(("EXTRACTED", "EVALUATING", "READY_TO_SUBMIT")),
            )))
            deadlines = []
            for vacancy in rows:
                data = vacancy.data or {}
                raw = data.get("model_retry_at")
                if not raw:
                    continue
                try:
                    value = datetime.fromisoformat(raw)
                except (TypeError, ValueError):
                    continue
                deadlines.append(
                    value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
                )
        if not deadlines:
            return True
        deadline = min(deadlines).timestamp()
        while True:
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if not item or cancellation_fence(db, session_id, self.generation):
                    return False
                if item.status in {
                    SessionStatus.STOPPING, SessionStatus.STOPPED,
                    SessionStatus.CANCELLED, SessionStatus.FAILED,
                    SessionStatus.COMPLETED,
                }:
                    return False
                paused = item.status == SessionStatus.PAUSED
            if paused:
                if not await self._wait_if_paused(session_id):
                    return False
                continue
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(remaining, 0.25))

    def _save_refs(self, session_id: int, refs: list[JobRef], adapter=None) -> list[JobRef]:
        """Persist discovery with a ten-item active evaluation window.

        Overflow lives in ``pipeline_checkpoints`` rather than the active
        queue.  Calling this with an empty list reconciles completed vacancy
        rows and promotes the next durable backlog slice.
        """
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if item is None:
                return []
            recovery = dict(item.recovery or {})
            legacy_refs: list[JobRef] = []
            for raw in recovery.get("pending_refs", []):
                try:
                    legacy_refs.append(JobRef.model_validate(raw))
                except (TypeError, ValueError):
                    continue
            unfinished = list(db.scalars(select(Vacancy).where(
                Vacancy.session_id == session_id,
                Vacancy.state.in_((
                    "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT",
                    "READY_TO_REPORT", "SUBMITTING",
                )),
            )))
            unfinished.sort(key=lambda vacancy: (vacancy.state != "SUBMITTING", vacancy.id))
            started_refs: list[JobRef] = []
            started_ids: set[str] = set()
            for vacancy in unfinished:
                if vacancy.external_id and vacancy.external_id not in started_ids:
                    started_refs.append(JobRef(external_id=vacancy.external_id, url=vacancy.url))
                    started_ids.add(vacancy.external_id)
            queued_by_id = {
                candidate.external_id: candidate
                for candidate in [*legacy_refs, *refs]
                if candidate.external_id not in started_ids
            }
            site_id = item.adapter_id

        active = self._pipeline().enqueue(
            session_id,
            site_id,
            [*started_refs, *queued_by_id.values()],
            generation=self.generation,
        )
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if item is None:
                return []
            recovery = dict(item.recovery or {})
            recovery["pending_refs"] = [candidate.model_dump(mode="json") for candidate in active]
            checkpoint = getattr(adapter, "search_checkpoint", None)
            if checkpoint:
                recovery["search_checkpoint"] = checkpoint()
            item.recovery = recovery
            search_metrics.flush(db, session_id)
            db.commit()
        return active

    def _record_submission_reconciliation_error(
        self, db, item, vacancy, *, message: str,
    ) -> None:
        """Persist a bounded reconciliation failure as a terminal vacancy error."""
        _record_vacancy_error(
            item,
            vacancy,
            "SUBMISSION_UNCONFIRMED",
            "Не удалось подтвердить отправку отклика после нескольких попыток",
        )
        existing = db.scalar(select(Application).where(Application.vacancy_id == vacancy.id))
        if existing is None:
            db.add(
                Application(
                    vacancy_id=vacancy.id,
                    status="unknown",
                )
            )
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
            self._record_submission_reconciliation_error(
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
        raw_adapter = None
        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if not item:
                return
            if item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.COMPLETED}:
                return
            was_paused = item.status == SessionStatus.PAUSED
            first_start = item.started_at is None
            if not was_paused:
                item.status = SessionStatus.RUNNING
            item.started_at = item.started_at or datetime.now(timezone.utc)
            if not was_paused:
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
            if item.adapter_id == "hirehi" and not (item.recovery or {}).get("search_version"):
                item.recovery = {
                    **(item.recovery or {}),
                    "search_version": _initial_hirehi_version(item),
                }
            db.commit()
            self.emit(db, session_id, "session", "Сессия запущена")
            if item.adapter_id == "hirehi":
                raw_adapter = adapter_registry.get(item.adapter_id)
            if not _hirehi_adaptive(item, raw_adapter) and _limit_reached(_application_count(item, item.adapter_id), item.application_limit):
                self.finalize(session_id, _application_limit_reason(item.adapter_id))
                return

        if raw_adapter is None:
            raw_adapter = adapter_registry.get(item.adapter_id)
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
            adapter_id = item.adapter_id
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
            adaptive_hirehi = _hirehi_adaptive(item, raw_adapter)
            if not adaptive_hirehi:
                search_metrics.initialize(
                    db, item, {}, selected_resumes,
                    algorithm=HIREHI_SEARCH_V1 if adapter_id == "hirehi" else None,
                    algorithm_version=HIREHI_SEARCH_V1 if adapter_id == "hirehi" else None,
                )

        adapter = RecoveryAdapter(raw_adapter)
        executor = get_browser(session_id)
        if not executor:
            executor = await restore_browser(session_id, adapter)

        if not await self._wait_if_paused(session_id):
            return

        login = await adapter.get_login_state(executor.page)
        if not login.authenticated:
            # Keep the login page open; automatically notice a restored login.
            await adapter._captcha(executor.page)
            raise AuthenticationPending("Ожидание восстановления авторизации на сайте")

        # Session-scoped imports use the normalized immutable snapshot payload
        # for every downstream consumer.
        if not selected_resumes:
            raise RecoverableFailure("Для оценки вакансий не выбрано ни одного резюме")

        gateway = self._model_gateway(session_id, adapter_id)
        if hasattr(gateway, "set_context"):
            gateway.set_context(stage="discovery")
        if preference_description and stored_policy is None:
            preference_policy = await compile_preference_policy(gateway, preference_description)
            with SessionLocal() as db:
                db.get(JobSession, session_id).preference_policy = preference_policy.model_dump(mode="json")
                db.commit()
        adaptive_engine = None
        hh_engine = None
        if adapter_id == "hh":
            hh_engine = AdaptiveSearch(adapter.adapter, gateway, selected_resumes, preference_policy)
            adapter = RecoveryAdapter(hh_engine)
        elif adaptive_hirehi:
            adaptive_engine = HireHiAdaptiveSearch(
                raw_adapter, gateway, selected_resumes, preference_policy,
                pro_enabled=bool(getattr(item, "hirehi_pro_enabled", False)),
                criteria_context={"minimum_scores": minimum_scores},
                minimum_scores=minimum_scores,
            )
            adapter = RecoveryAdapter(adaptive_engine)
        hirehi_category: str | None = None
        hirehi_grade_values: list[str] | None = None
        with SessionLocal() as db:
            search_filters = (db.get(JobSession, session_id).recovery or {}).get("search_filters")
            saved_cursor = (db.get(JobSession, session_id).recovery or {}).get("search_checkpoint")
        if adaptive_hirehi:
            # Adaptive HireHi owns its portfolio and opens one validated
            # source at a time.  Persist only the engine identity; never
            # invoke the legacy category/grades chooser in this branch.
            search_filters = {"search_version": HIREHI_SEARCH_ADAPTIVE_V3}
        elif search_filters is not None:
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
        # Restore the adaptive engine before opening search.  An empty
        # portfolio makes ``open_search`` plan a fresh portfolio, which would
        # overwrite the durable checkpoint and inflate planner/D/N/R metrics.
        restore_cursor = getattr(adapter, "restore_search_checkpoint", None)
        restored_from_checkpoint = False
        if saved_cursor is not None and restore_cursor:
            try:
                await _await_if_needed(restore_cursor(saved_cursor))
            except (ValueError, TypeError, KeyError) as exc:
                if not adaptive_hirehi:
                    raise
                with SessionLocal() as db:
                    item = db.get(JobSession, session_id)
                    recovery = dict(item.recovery or {})
                    recovery.pop("search_checkpoint", None)
                    item.recovery = recovery
                    self.emit(
                        db, session_id, "checkpoint_discarded",
                        "Адаптивный checkpoint не прошёл проверку; начинаем безопасный сбор заново",
                        {"kind": "invalid_adaptive_checkpoint", "error_type": type(exc).__name__},
                    )
                    db.commit()
                saved_cursor = None
            else:
                restored_from_checkpoint = True

        await adapter.open_search(executor.page, search_filters)
        if adaptive_engine is not None:
            engine_checkpoint = adaptive_engine.search_checkpoint()
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                search_metrics.initialize(
                    db, item, {}, selected_resumes,
                    algorithm=HIREHI_SEARCH_ADAPTIVE_V3,
                    algorithm_version=HIREHI_SEARCH_ADAPTIVE_V3,
                    criteria_hash=engine_checkpoint["criteria_hash"],
                )
                db.commit()
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
        if restored_from_checkpoint:
            # The restored engine owns pending refs and continuation state;
            # don't collect the initial listing a second time.
            refs = []
        else:
            refs = await adapter.collect_job_refs(executor.page)
        if adaptive_engine is not None and not restored_from_checkpoint:
            try:
                prior_loader = getattr(search_metrics, "load_hirehi_weak_prior", None)
                if prior_loader:
                    with SessionLocal() as db:
                        _apply_hirehi_weak_prior(
                            adaptive_engine,
                            prior_loader(db, db.get(JobSession, session_id)),
                        )
            except (TypeError, ValueError, KeyError):
                # Prior data is an optional optimization; malformed old
                # measurements must not prevent a fresh adaptive search.
                pass
        collect_more = getattr(adapter, "collect_more_job_refs", None)
        # Track refs handed to the local active window, not every discovered
        # ref: overflow remains durable and must become eligible after the
        # current window reaches terminal vacancy states.
        seen_ref_ids: set[str] = set()
        if not refs and collect_more is not None and saved_cursor is None:
            refs.extend(await collect_more(executor.page))
        refs = self._save_refs(session_id, refs, adapter)
        if adaptive_engine is not None:
            with SessionLocal() as db:
                search_metrics.record("hirehi_snapshot", _hirehi_snapshot_data(adaptive_engine))
                search_metrics.flush(db, session_id)
                db.commit()
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

        async def wait_adaptive_retry() -> bool:
            """Sleep in short cancellable slices while sources cool down."""
            if adaptive_engine is None:
                return True
            delay = float(getattr(adaptive_engine, "next_retry_delay", lambda: 1.0)())
            deadline = asyncio.get_running_loop().time() + min(128.0, max(0.1, delay))
            while True:
                with SessionLocal() as db:
                    current = db.get(JobSession, session_id)
                    if not current or current.status in {
                        SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED,
                        SessionStatus.FAILED, SessionStatus.COMPLETED,
                    }:
                        return False
                    if current.status == SessionStatus.PAUSED:
                        return await self._wait_if_paused(session_id)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return True
                await asyncio.sleep(min(0.25, remaining))

        async def wait_hh_refresh() -> bool:
            """Keep an exhausted HH session running until its durable refresh is due."""
            delay = max(0.1, float(hh_engine.next_retry_delay()))
            if hh_engine.search_exhausted:
                message = f"Все текущие источники HH проверены. Поиск обновится автоматически через {int(delay + 0.999)} с."
            else:
                message = f"Источник HH временно недоступен. Повторная проверка через {int(delay + 0.999)} с."
            with SessionLocal() as db:
                current = db.get(JobSession, session_id)
                if not current:
                    return False
                recovery = dict(current.recovery or {})
                if recovery.get("message") != message:
                    recovery["message"] = message
                    current.recovery = recovery
                    current.stop_reason = message
                    self.emit(db, session_id, "discovery_wait", message, {
                        "delay_seconds": round(delay, 2),
                        "refresh_attempt": hh_engine.epoch_refresh_attempt,
                    })
                    db.commit()
            deadline = asyncio.get_running_loop().time() + delay
            while True:
                with SessionLocal() as db:
                    current = db.get(JobSession, session_id)
                    if not current or current.status in {
                        SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED,
                        SessionStatus.FAILED, SessionStatus.COMPLETED,
                    }:
                        return False
                    paused = current.status == SessionStatus.PAUSED
                if paused and not await self._wait_if_paused(session_id):
                    return False
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return True
                await asyncio.sleep(min(0.25, remaining))

        def clear_hh_wait_reason() -> None:
            with SessionLocal() as db:
                current = db.get(JobSession, session_id)
                message = (current.recovery or {}).get("message") if current else None
                if current and isinstance(message, str) and message.startswith((
                    "Все текущие источники HH проверены.", "Источник HH временно недоступен."
                )):
                    recovery = dict(current.recovery)
                    recovery["message"] = None
                    current.recovery = recovery
                    current.stop_reason = None
                    db.commit()

        def mark_adaptive_source_unavailable() -> None:
            """Turn a closed Free/PRO modal into a bounded unavailable arm."""
            if adaptive_engine is None:
                return
            raw = getattr(adaptive_engine, "adapter", None)
            if not bool(getattr(raw, "discovery_unavailable", False)):
                return
            batch = getattr(adaptive_engine, "last_discovery_batch", None) or {}
            source = getattr(adaptive_engine, "scheduler", None)
            source = getattr(source, "sources", {}).get(batch.get("source")) if source else None
            scheduler = getattr(adaptive_engine, "scheduler", None)
            if source is not None and scheduler is not None:
                source.availability = 0.1
                source.exhausted = True
                source.next_refresh = scheduler.turn + 128

        async def refill_if_exhausted() -> bool:
            """Refill only after rechecking state and session limits."""
            nonlocal collect_more, completion_reason
            # First drain durable overflow into the bounded active window.
            # Browser discovery resumes only when that window has free slots.
            promoted = self._save_refs(session_id, [], adapter)
            promoted = [
                candidate for candidate in promoted
                if candidate.external_id not in seen_ref_ids
            ]
            if promoted:
                refs.extend(promoted)
                seen_ref_ids.update(candidate.external_id for candidate in promoted)
                return True
            if collect_more is None:
                return False
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                if not item:
                    collect_more = None
                    return False
                if item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.FAILED}:
                    collect_more = None
                    return False
                if not adaptive_hirehi and _limit_reached(_application_count(item, adapter_id), item.application_limit):
                    completion_reason = _application_limit_reason(adapter_id)
                    collect_more = None
                    return False
            if hh_engine is not None:
                while True:
                    if not await self._wait_if_paused(session_id):
                        collect_more = None
                        return False
                    if hh_engine.search_exhausted and not await wait_hh_refresh():
                        collect_more = None
                        return False
                    # Persist only accepted work; duplicate-only and empty
                    # pages still advance the HH source cursor and may lead
                    # to another source or a paced whole-epoch refresh.
                    next_refs = await collect_more(executor.page)
                    accepted_refs = self._save_refs(session_id, next_refs, adapter)
                    new_refs = [candidate for candidate in accepted_refs
                                if candidate.external_id not in seen_ref_ids]
                    if new_refs:
                        refs.extend(new_refs)
                        seen_ref_ids.update(candidate.external_id for candidate in new_refs)
                        hh_engine.reset_epoch_backoff_after_durable_work()
                        self._save_refs(session_id, [], adapter)
                        clear_hh_wait_reason()
                        return True
                    if hh_engine.search_exhausted:
                        # The checkpoint written by _save_refs includes the
                        # epoch refresh deadline and exact-ID dedup state.
                        continue
                    batch = hh_engine.last_discovery_batch or {}
                    if batch.get("source") == "source_retry_wait" and not await wait_hh_refresh():
                        collect_more = None
                        return False
            if adaptive_engine is not None:
                # Adaptive HireHi is open-ended. Empty and duplicate-only
                # batches advance the durable source scheduler, then yield
                # to its cooldown before trying another source.
                while True:
                    if not await self._wait_if_paused(session_id):
                        collect_more = None
                        return False
                    next_refs = await collect_more(executor.page)
                    mark_adaptive_source_unavailable()
                    accepted_refs = self._save_refs(session_id, next_refs, adapter)
                    new_refs = [
                        candidate for candidate in accepted_refs
                        if candidate.external_id not in seen_ref_ids
                    ]
                    if new_refs:
                        refs.extend(new_refs)
                        seen_ref_ids.update(candidate.external_id for candidate in new_refs)
                        return True
                    if not await wait_adaptive_retry():
                        collect_more = None
                        return False
            # ``enqueue`` is the capacity gate. If ten items remain active,
            # this call is skipped on the next pass until evaluation drains.
            next_refs = await collect_more(executor.page)
            mark_adaptive_source_unavailable()
            accepted_refs = self._save_refs(session_id, next_refs, adapter)
            while not next_refs and not getattr(adapter, "search_exhausted", True):
                if not await self._wait_if_paused(session_id):
                    collect_more = None
                    return False
                next_refs = await collect_more(executor.page)
                accepted_refs = self._save_refs(session_id, next_refs, adapter)
            new_refs = [
                candidate for candidate in accepted_refs
                if candidate.external_id not in seen_ref_ids
            ]
            if new_refs:
                refs.extend(new_refs)
                seen_ref_ids.update(ref.external_id for ref in new_refs)
                return True
            elif getattr(adapter, "search_exhausted", True):
                collect_more = None
            return False

        hh_prefetched: dict[str, tuple[Any, asyncio.Task]] = {}
        browser_ref_external_id: str | None = None
        if adapter_id == "hh" and refs:
            # Keep browser access strictly serial while overlapping model
            # evaluation with extraction of the next vacancies. The admitted
            # ref list is already capped by EVALUATION_QUEUE_LIMIT.
            for candidate in refs[:EVALUATION_QUEUE_LIMIT]:
                if not await self._wait_if_paused(session_id):
                    break
                with SessionLocal() as db:
                    item = db.get(JobSession, session_id)
                    if (
                        not item
                        or cancellation_fence(db, session_id, self.generation)
                        or _limit_reached(_application_count(item, adapter_id), item.application_limit)
                    ):
                        break
                    existing = db.scalar(select(Vacancy).where(
                        *_vacancy_scope(adapter_id, adapter.site_id, candidate.external_id, session_id)
                    ))
                    # Retries and persisted artifacts belong to the recovery
                    # path below; a prefetch must never bypass duplicate fences.
                    if existing is not None:
                        continue
                try:
                    await adapter.open_job(executor.page, candidate)
                    browser_ref_external_id = candidate.external_id
                    if await adapter.detect_blockers(executor.page):
                        break
                    source_posting = await adapter.extract_job(executor.page)
                    posting = sanitize_untrusted_input(source_posting, context="vacancy")
                except (CaptchaRequired, PromptInjectionDetected):
                    raise
                except Exception:
                    # Let the normal per-vacancy extraction path persist its
                    # bounded retry outcome and diagnostic event.
                    break

                model_gateway = self._model_gateway(session_id, adapter_id)
                if hasattr(model_gateway, "set_context"):
                    model_gateway.set_context(
                        pipeline_key=candidate.external_id,
                        stage="evaluation",
                    )

                async def evaluate_prefetched(
                    current_posting=posting,
                    current_gateway=model_gateway,
                ):
                    if not await self._wait_if_paused(session_id):
                        raise asyncio.CancelledError
                    with SessionLocal() as fence_db:
                        owner = fence_db.get(JobSession, session_id)
                        if (
                            not owner
                            or owner.status != SessionStatus.RUNNING
                            or _limit_reached(
                                _application_count(owner, adapter_id),
                                owner.application_limit,
                            )
                            or cancellation_fence(fence_db, session_id, self.generation)
                        ):
                            raise asyncio.CancelledError
                    started = perf_counter()
                    outcome = await evaluate(
                        current_posting,
                        profile,
                        selected_resumes,
                        current_gateway,
                        minimum_scores,
                        preference_policy,
                    )
                    search_metrics.record("model", {
                        "stage": "evaluation",
                        "seconds": perf_counter() - started,
                        "outcome": "ok",
                    })
                    if not await self._wait_if_paused(session_id):
                        raise asyncio.CancelledError
                    with SessionLocal() as fence_db:
                        if cancellation_fence(fence_db, session_id, self.generation):
                            raise asyncio.CancelledError
                    return outcome

                evaluation_task = asyncio.create_task(evaluate_prefetched())
                active_tasks = self._model_prefetch_tasks.setdefault(session_id, set())
                active_tasks.add(evaluation_task)

                def finish_prefetch(done_task, *, owned=active_tasks):
                    owned.discard(done_task)
                    if not done_task.cancelled():
                        done_task.exception()

                evaluation_task.add_done_callback(finish_prefetch)
                hh_prefetched[candidate.external_id] = (source_posting, evaluation_task)
                # Let the model request enter the broker queue before the
                # main loop begins its sequential handling of this vacancy.
                await asyncio.sleep(0)

        ref_index = 0
        deferred_model_work = False
        while True:
            if ref_index >= len(refs):
                if deferred_model_work:
                    deferred_model_work = False
                    if not await self._wait_for_model_retry(session_id):
                        return
                    ref_index = 0
                    continue
                if retry_needed:
                    # Retry unfinished work before asking an open-ended
                    # discovery source for more refs. Otherwise a full active
                    # window containing failed items can never drain.
                    raise RecoverableFailure(
                        "Не все найденные вакансии обработаны; повторяем временные сбои"
                    )
                # A source can fill the bounded active window in one call and
                # leave the rest in the durable checkpoint. Keep draining that
                # backlog even after discovery reports exhaustion.
                if not await refill_if_exhausted():
                    break
                continue
            ref = refs[ref_index]
            ref_index += 1
            prefetched = hh_prefetched.pop(ref.external_id, None)
            prefetched_evaluation_task = prefetched[1] if prefetched else None
            if not await self._wait_if_paused(session_id):
                break
            with SessionLocal() as db:
                item = db.get(JobSession, session_id)
                application_limit = item.application_limit
                if not adaptive_hirehi and _limit_reached(_application_count(item, adapter_id), application_limit):
                    completion_reason = _application_limit_reason(adapter_id)
                    break
                existing = db.scalar(select(Vacancy).where(
                    *_vacancy_scope(adapter_id, adapter.site_id, ref.external_id, session_id)
                ))
                skip_existing = bool(existing and (existing.session_id != session_id or existing.state not in {
                    "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING",
                }))
                existing_vacancy_id = existing.id if skip_existing else None
                if skip_existing and existing.session_id != session_id:
                    observer = getattr(adapter, "observe_overlap", None)
                    if observer:
                        observer(ref.external_id)
                    search_metrics.record("overlap", {"external_id": ref.external_id})
                    search_metrics.flush(db, session_id)
                    db.commit()
            if skip_existing:
                # The durable queue belongs to this session even when the
                # vacancy record belongs to an older session. Mark only the
                # current pipeline item terminal; never rewrite old vacancy
                # data or application counters.
                self._complete_duplicate_pipeline_item(
                    session_id, adapter.site_id, ref.external_id, existing_vacancy_id,
                )
                continue
            source_posting = None
            posting_was_sanitized = False
            recovered_from_artifact = False
            saved_posting_data = None
            if adapter_id == "hh" and prefetched is None:
                # Evaluation and letter stages are model work over the durable
                # extracted vacancy snapshot. On a retry, reuse that artifact
                # and defer browser navigation until the application stage.
                with SessionLocal() as artifact_db:
                    saved_vacancy = artifact_db.scalar(select(Vacancy).where(
                        *_vacancy_scope(adapter_id, adapter.site_id, ref.external_id, session_id)
                    ))
                    if saved_vacancy and saved_vacancy.state in {
                        "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT",
                    } and isinstance(saved_vacancy.data, dict):
                        saved_posting_data = dict(saved_vacancy.data)
            try:
                processing_started = perf_counter()
                self._advance_pipeline(
                    session_id, adapter.site_id, ref.external_id, "extraction"
                )
                if prefetched is not None:
                    source_posting = prefetched[0]
                    posting = sanitize_untrusted_input(source_posting, context="vacancy")
                    original_payload = source_posting.model_dump(mode="json")
                    working_payload = posting.model_dump(mode="json")
                    posting_was_sanitized = original_payload != working_payload
                    recovered_from_artifact = True
                elif saved_posting_data is not None:
                    source_posting = domain_schemas.JobPosting.model_validate(saved_posting_data)
                    posting = sanitize_untrusted_input(source_posting, context="vacancy")
                    original_payload = source_posting.model_dump(mode="json")
                    working_payload = posting.model_dump(mode="json")
                    posting_was_sanitized = original_payload != working_payload
                    recovered_from_artifact = True
                else:
                    await adapter.open_job(executor.page, ref)
                    browser_ref_external_id = ref.external_id
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
            skip_duplicate_posting = False
            duplicate_vacancy_id = None
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
                    skip_duplicate_posting = True
                    duplicate_vacancy_id = existing.id
                elif existing and existing.state not in {
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
                    skip_duplicate_posting = True
                    duplicate_vacancy_id = existing.id
            if skip_duplicate_posting:
                self._complete_duplicate_pipeline_item(
                    session_id, adapter.site_id, ref.external_id, duplicate_vacancy_id,
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
                    persisted_progress = (vacancy.data or {}).get("submission_progress", {})
                    if (
                        adapter_id == "hh"
                        and isinstance(persisted_progress, dict)
                        and persisted_progress.get("cv_confirmed")
                        and persisted_progress.get("cover_letter_pending")
                        and callable(getattr(adapter, "verify_cv_submission", None))
                        and callable(getattr(adapter, "resume_application", None))
                    ):
                        # HH may have accepted the CV before the letter upload
                        # was interrupted. Reconcile that fact, then resume
                        # only the letter step from the saved plan.
                        verified_cv = await adapter.verify_cv_submission(executor.page)
                        if verified_cv.status not in {"submitted", "already_applied"}:
                            if verified_cv.status == "blocked" and verified_cv.confirmed:
                                self._record_submission(db, item, vacancy, verified_cv)
                            else:
                                retry_needed = True
                            db.commit()
                            continue
                        recovered_plan_record = db.scalar(select(ApplicationPlanRecord).where(
                            ApplicationPlanRecord.vacancy_id == vacancy.id
                        ))
                        if not recovered_plan_record or not _cache_matches_resume(
                            recovered_plan_record.data, resume_content_hash
                        ):
                            self._record_submission_reconciliation_error(
                                db, item, vacancy,
                                message="Не удалось восстановить письмо после подтверждения резюме на HH",
                            )
                            continue
                        try:
                            recovered_plan = ApplicationPlan.model_validate(recovered_plan_record.data)
                            if private_context:
                                recovered_plan = _render_private_plan(recovered_plan, private_context)
                            _assert_safe_application_plan(
                                recovered_plan, profile,
                                [*selected_resumes, private_context] if private_context else selected_resumes,
                                context="recovered_hh_letter_plan",
                            )
                            await adapter.resume_application(
                                executor.page,
                                recovered_plan,
                                cv_confirmed=True,
                                cover_letter_pending=True,
                            )
                            def recovered_checkpoint(
                                current_plan,
                                current_record=recovered_plan_record,
                            ):
                                if cancellation_fence(db, session_id, self.generation):
                                    return False
                                plan_data = (
                                    _redact_plan(current_plan, private_context)
                                    if private_context else current_plan.model_dump()
                                )
                                if resume_content_hash:
                                    plan_data[_RESUME_HASH_KEY] = resume_content_hash
                                current_record.data = plan_data
                                db.commit()
                                return True

                            resumed_outcome = await complete_application(
                                adapter, executor.page, recovered_plan, posting,
                                profile, selected_resumes, preference_description,
                                gateway, recovered_checkpoint,
                                guaranteed_application=item.guaranteed_application,
                                private_view=private_context or None,
                            )
                            if resumed_outcome.stopped:
                                return
                            if resumed_outcome.error_code:
                                _record_vacancy_error(
                                    item, vacancy, resumed_outcome.error_code,
                                    resumed_outcome.error_message or "Не удалось продолжить отправку письма на HH",
                                )
                                db.commit()
                                continue
                            submission = resumed_outcome.submission
                            if submission is None:
                                retry_needed = True
                                db.commit()
                                continue
                        except PromptInjectionDetected as exc:
                            _record_security_incident(
                                db, item, vacancy, exc,
                                context="recovered_hh_letter", emit=self.emit,
                            )
                            db.commit()
                            continue
                        finally:
                            progress_reader = getattr(adapter, "get_submission_progress", None)
                            if callable(progress_reader):
                                progress = progress_reader()
                                if isinstance(progress, dict):
                                    vacancy.data = {
                                        **(vacancy.data or {}),
                                        "submission_progress": {
                                            key: bool(progress.get(key)) for key in (
                                                "cv_confirmed", "cover_letter_pending",
                                                "cover_letter_confirmed",
                                            )
                                        },
                                    }
                                    db.commit()
                        if self._record_submission(db, item, vacancy, submission):
                            retry_needed = True
                        db.commit()
                        continue
                    # First reconcile the durable submit attempt against the
                    # site. A confirmed result needs no cached plan at all.
                    verifier = getattr(adapter, "verify_submission", None)
                    if not callable(verifier):
                        self._record_submission_reconciliation_error(
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
                    # A retry may send data, so validate its cached plan and
                    # current form only after the read-only site check failed
                    # to confirm a completed submission.
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
                            if private_context:
                                recovered_plan = _render_private_plan(recovered_plan, private_context)
                            _assert_safe_application_plan(
                                recovered_plan, profile,
                                [*selected_resumes, private_context] if private_context else selected_resumes,
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
                                    [*selected_resumes, private_context] if private_context else selected_resumes,
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
                            break
                        continue
                    data = dict(vacancy.data or {})
                    try:
                        attempts = max(0, int(data.get("submission_reconciliation_attempts", 0)))
                    except (TypeError, ValueError):
                        attempts = 0
                    attempts += 1
                    if attempts >= _SUBMISSION_RECONCILIATION_LIMIT:
                        self._record_submission_reconciliation_error(
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
                    # The site confirmed absence. Fall through and retry this
                    # vacancy immediately; never let later refs overtake it.

                vacancy.state = "EVALUATING"
                db.commit()
                self._advance_pipeline(
                    session_id,
                    adapter.site_id,
                    posting.external_id,
                    "evaluation",
                    vacancy_id=vacancy.id,
                )
                if hasattr(gateway, "set_context"):
                    gateway.set_context(
                        vacancy_id=vacancy.id,
                        pipeline_key=posting.external_id,
                stage="evaluation",
                    )
                evaluation_record = db.scalar(
                    select(Evaluation).where(Evaluation.vacancy_id == vacancy.id)
                )
                refresh_cached_evaluation = False
                semantic_reused = False
                semantic_reuse_info = None
                if evaluation_record is None and adaptive_engine is not None:
                    candidate = getattr(adaptive_engine, "semantic_reuse", lambda _ref: None)(posting)
                    selected_ids = set((getattr(adaptive_engine, "audit_sample_state", {}) or {}).get("selected", []))
                    if candidate and posting.external_id not in selected_ids:
                        representative_id = str(candidate.get("representative_id", ""))
                        representative = db.scalar(select(Vacancy).where(
                            Vacancy.session_id == session_id,
                            Vacancy.external_id == representative_id,
                        ))
                        representative_eval = (
                            db.scalar(select(Evaluation).where(Evaluation.vacancy_id == representative.id))
                            if representative is not None else None
                        )
                        if representative_eval is not None:
                            try:
                                reused_result = JobEvaluation.model_validate(representative_eval.data)
                                assert_safe_output(reused_result, context="semantic_reused_evaluation")
                            except (TypeError, ValueError, PromptInjectionDetected):
                                reused_result = None
                            if reused_result is not None:
                                result = reused_result
                                semantic_reused = True
                                semantic_reuse_info = {
                                    "external_id": posting.external_id,
                                    "canonical_external_id": representative_id,
                                    "near_duplicate": True,
                                    "reused": True,
                                }
                                self.emit(db, session_id, "semantic_reuse",
                                          "\u041e\u0446\u0435\u043d\u043a\u0430 \u043f\u0435\u0440\u0435\u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0430 \u0434\u043b\u044f \u0441\u0435\u043c\u0430\u043d\u0442\u0438\u0447\u0435\u0441\u043a\u043e\u0433\u043e \u0434\u0443\u0431\u043b\u0438\u043a\u0430", semantic_reuse_info)
                                search_metrics.record("semantic_reuse", semantic_reuse_info)
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
                if (evaluation_record is None and not semantic_reused) or refresh_cached_evaluation:
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
                        use_prefetched_result = prefetched_evaluation_task is not None

                        async def evaluate_current(
                            current_task=prefetched_evaluation_task,
                            current_posting=posting,
                            current_gateway=gateway,
                            current_scores=minimum_scores,
                            current_policy=preference_policy,
                        ):
                            nonlocal use_prefetched_result
                            if use_prefetched_result:
                                use_prefetched_result = False
                                return await current_task
                            return await evaluate(
                                current_posting, profile, selected_resumes, current_gateway,
                                current_scores, current_policy,
                            )

                        succeeded, result = await self._model_stage_call(
                            db, item, vacancy, "evaluation", evaluate_current
                        )
                        if not succeeded:
                            db.refresh(item)
                            if item.status != SessionStatus.RUNNING:
                                return
                            db.refresh(vacancy)
                            if vacancy.state != "ERROR":
                                deferred_model_work = True
                            continue
                        assert_safe_output(result, context="evaluation")
                    except PromptInjectionDetected as exc:
                        _record_security_incident(
                            db, item, vacancy, exc, context="evaluation", emit=self.emit
                        )
                        db.commit()
                        continue
                    except ModelPermanentError as exc:
                        _record_vacancy_error(
                            item, vacancy, "VACANCY_PROCESSING_FAILED",
                            f"Постоянная ошибка модели на этапе оценки ({type(exc).__name__})",
                        )
                        self.emit(
                            db, session_id, "vacancy_error",
                            "Вакансия пропущена: модель вернула постоянную ошибку оценки",
                            {"vacancy_id": vacancy.id, "stage": "evaluation", "error_type": type(exc).__name__},
                        )
                        db.commit()
                        continue
                db.refresh(item)
                if item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
                    return
                if evaluation_record is None:
                    evaluation_data = result.model_dump()
                    if semantic_reuse_info:
                        evaluation_data.update({
                            "canonical_external_id": semantic_reuse_info["canonical_external_id"],
                            "semantic_reused": True,
                        })
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
                            "semantic_reused": semantic_reused,
                            "minimum_score_violations": result.minimum_score_violations,
                            "breakdown": [row.model_dump() for row in result.score_breakdown],
                        },
                    )
                observer = getattr(adapter, "observe", None)
                if observer:
                    if adaptive_engine is not None:
                        await observer(
                            executor.page, posting, result.decision,
                            perf_counter() - processing_started,
                            reason=_hirehi_evaluation_reason(result),
                        )
                    else:
                        await observer(executor.page, posting, result.decision, perf_counter() - processing_started)
                    if adaptive_engine is not None:
                        await adaptive_engine.record_audit_verdict(
                            posting, result.decision,
                            seconds=perf_counter() - processing_started,
                        )
                    # Feedback changes the scheduler, not the discovery queue.
                    # Avoid rescanning all vacancies after every evaluation.
                    item.recovery = {**(item.recovery or {}), "search_checkpoint": adapter.search_checkpoint()}
                    if adaptive_engine is not None:
                        search_metrics.record("hirehi_snapshot", _hirehi_snapshot_data(adaptive_engine))
                    search_metrics.flush(db, session_id)
                    db.commit()
                if result.decision == "skip":
                    vacancy.state = "REJECTED_BY_MODEL"
                    counters = dict(item.counters)
                    counters["filtered"] += 1
                    item.counters = counters
                else:
                    self._advance_pipeline(
                        session_id,
                        adapter.site_id,
                        posting.external_id,
                        "letter",
                        vacancy_id=vacancy.id,
                    )
                    if hasattr(gateway, "set_context"):
                        gateway.set_context(
                            vacancy_id=vacancy.id,
                            pipeline_key=posting.external_id,
                            stage="letter",
                        )
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
                        if private_context:
                            plan = _render_private_plan(plan, private_context)
                        try:
                            _assert_safe_application_plan(
                                plan, profile,
                                [*selected_resumes, private_context] if private_context else selected_resumes,
                                context="cached_application_plan",
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
                            async def generate_letter(
                                current_posting=posting,
                                current_writer_profile=writer_profile,
                                current_gateway=gateway,
                                current_policy=preference_policy,
                                current_kwargs=letter_kwargs,
                            ):
                                return await write_cover_letter(
                                    current_posting,
                                    current_writer_profile,
                                    selected_resumes,
                                    current_gateway,
                                    current_policy,
                                    **current_kwargs,
                                )

                            succeeded, letter = await self._model_stage_call(
                                db, item, vacancy, "letter", generate_letter
                            )
                            if not succeeded:
                                db.refresh(item)
                                if item.status != SessionStatus.RUNNING:
                                    return
                                db.refresh(vacancy)
                                if vacancy.state != "ERROR":
                                    deferred_model_work = True
                                continue
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
                            if adapter_id != "hh" and attempts < _COVER_LETTER_RETRY_LIMIT:
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
                        except ModelPermanentError as exc:
                            _record_vacancy_error(
                                item, vacancy, "VACANCY_PROCESSING_FAILED",
                                f"Постоянная ошибка модели на этапе письма ({type(exc).__name__})",
                            )
                            self.emit(
                                db, session_id, "vacancy_error",
                                "Вакансия пропущена: модель вернула постоянную ошибку письма",
                                {"vacancy_id": vacancy.id, "stage": "letter", "error_type": type(exc).__name__},
                            )
                            db.commit()
                            continue
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
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
                    next_stage = "reporting" if adapter_id == "hirehi" else "submission"
                    if (
                        recovered_from_artifact
                        and adapter_id == "hh"
                        and browser_ref_external_id != posting.external_id
                    ):
                        # Model recovery used the stored vacancy snapshot. Open
                        # the posting only now, when the browser is needed for
                        # the form and site-side submission reconciliation.
                        allowed, _ = await _guarded_representational_call(
                            db,
                            session_id,
                            self.generation,
                            lambda current_ref=ref: adapter.open_job(
                                executor.page, current_ref
                            ),
                        )
                        if not allowed:
                            return
                        browser_ref_external_id = posting.external_id
                        recovered_from_artifact = False
                    self._advance_pipeline(
                        session_id,
                        adapter.site_id,
                        posting.external_id,
                        next_stage,
                        vacancy_id=vacancy.id,
                    )
                    if hasattr(gateway, "set_context"):
                        gateway.set_context(
                            vacancy_id=vacancy.id,
                            pipeline_key=posting.external_id,
                            stage=next_stage,
                        )
                    db.refresh(item)
                    if item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
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
                            db.refresh(item)
                            if cancellation_fence(db, session_id, self.generation) or item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
                                return
                            allowed, form = await _guarded_representational_call(
                                db,
                                session_id,
                                self.generation,
                                lambda: adapter.open_application(executor.page),
                            )
                            if not allowed:
                                return
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
                            if cancellation_fence(db, session_id, self.generation):
                                return
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
                                if cancellation_fence(db, session_id, self.generation) or item.status in {
                                    SessionStatus.STOPPING,
                                    SessionStatus.STOPPED,
                                    SessionStatus.CANCELLED,
                                    SessionStatus.PAUSED,
                                }:
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
                            db.refresh(item)
                            if cancellation_fence(db, session_id, self.generation):
                                return
                            try:
                                allowed, outcome = await _guarded_representational_call(
                                    db,
                                    session_id,
                                    self.generation,
                                    lambda adapter=adapter, page=executor.page, current_plan=plan,
                                    current_posting=posting, current_profile=profile,
                                    current_resumes=selected_resumes, current_preference=preference_description,
                                    current_gateway=gateway, current_checkpoint=checkpoint,
                                    guaranteed=item.guaranteed_application,
                                    private=private_context: complete_application(
                                        adapter, page, current_plan, current_posting, current_profile,
                                        current_resumes, current_preference, current_gateway, current_checkpoint,
                                        guaranteed_application=guaranteed,
                                        private_view=private or None,
                                    ),
                                )
                            finally:
                                progress_reader = getattr(adapter, "get_submission_progress", None)
                                if adapter_id == "hh" and callable(progress_reader):
                                    progress = progress_reader()
                                    if isinstance(progress, dict):
                                        vacancy.data = {
                                            **(vacancy.data or {}),
                                            "submission_progress": {
                                                key: bool(progress.get(key)) for key in (
                                                    "cv_confirmed", "cover_letter_pending",
                                                    "cover_letter_confirmed",
                                                )
                                            },
                                        }
                                        db.commit()
                            if not allowed:
                                return
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
                            if retry_needed:
                                break
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
                        if item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
                            return
                        db.refresh(item)
                        if cancellation_fence(db, session_id, self.generation) or item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
                            return
                        allowed, result = await _guarded_representational_call(
                            db,
                            session_id,
                            self.generation,
                            lambda current_plan=plan: adapter.fill_application(executor.page, current_plan),
                        )
                        if not allowed:
                            return
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
                        if item.status in {SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
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
                            db.refresh(item)
                            if cancellation_fence(db, session_id, self.generation) or item.status in {SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED, SessionStatus.PAUSED}:
                                return
                            allowed, submission = await _guarded_representational_call(
                                db,
                                session_id,
                                self.generation,
                                lambda: adapter.submit_application(executor.page),
                            )
                            if not allowed:
                                return
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
                        if retry_needed:
                            break
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
                    item.recovery.pop(_RECOVERY_COUNTERS_KEY, None)
                    db.commit()
            await asyncio.sleep(0.05)

        with SessionLocal() as db:
            item = db.get(JobSession, session_id)
            if item is None or item.status in {
                SessionStatus.STOPPING, SessionStatus.STOPPED, SessionStatus.CANCELLED,
                SessionStatus.PAUSED, SessionStatus.FAILED, SessionStatus.COMPLETED,
            }:
                return
            if not adaptive_hirehi and _limit_reached(_application_count(item, adapter_id), item.application_limit):
                completion_reason = _application_limit_reason(adapter_id)
            elif retry_needed:
                # Every exit from the processing loop must resolve unfinished
                # work before applying the continuous-search completion policy.
                # A successful later submission can break this pass while an
                # earlier extraction still needs its retry.
                raise RecoverableFailure("Не все найденные вакансии обработаны; повторяем временные сбои")
            elif adaptive_engine is not None or hh_engine is not None:
                # Continuous sources have no natural terminal exhaustion.
                return
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
