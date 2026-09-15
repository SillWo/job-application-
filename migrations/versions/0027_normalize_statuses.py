"""Normalize vacancy/session statuses and remove legacy manual-review data."""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

CANONICAL_VACANCY_STATES = frozenset(
    {
        "EXTRACTED",
        "EVALUATING",
        "REJECTED_BY_MODEL",
        "READY_TO_SUBMIT",
        "SUBMITTING",
        "SUBMITTED",
        "ALREADY_APPLIED",
        "READY_TO_REPORT",
        "REPORTED",
        "UNCONFIRMED",
        "ERROR",
    }
)
GENERIC_ERROR_CODE = "VACANCY_PROCESSING_FAILED"
GENERIC_ERROR_MESSAGE = "Вакансия не обработана из-за ошибки"
WAITING_FOR_LOGIN_STOP_REASON = "Ожидание входа в аккаунт требует продолжения"
CANONICAL_ERROR_CODES = frozenset(
    {
        "APPLICATION_FORM_UNRESOLVED",
        "APPLICATION_FORM_UNSUPPORTED",
        "APPLICATION_FORM_STUCK",
        "FOREIGN_APPLICATION_CONFIRMATION_FAILED",
        "UNKNOWN_APPLICATION_ROUTE",
        "SUBMISSION_BLOCKED",
        "MFA_REQUIRED",
        "SUBMISSION_UNCONFIRMED",
        "SECURITY_BLOCKED",
        "SITE_ACCESS_BLOCKED",
        GENERIC_ERROR_CODE,
    }
)
MAX_LEGACY_ERROR_MESSAGE_LENGTH = 1000
_UNTRUSTED_ERROR_MESSAGE = re.compile(
    r"(?:ignore|disregard|forget|override|reveal|show|print|system\s+prompt|"
    r"developer\s+(?:message|prompt)|api\s*key|password|secret|"
    r"игнорир\w*|забуд\w*|раскро\w*|системн\w*\s+промпт|инструкц\w*)",
    re.IGNORECASE,
)


def _json(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _rename_error_reasons(data: dict) -> dict:
    old = data.pop("application_review_reasons", None)
    reasons = data.get("application_error_reasons")
    if not isinstance(reasons, list):
        reasons = []
    for values in (old,):
        if isinstance(values, list):
            reasons.extend(value for value in values if value not in reasons)
    if reasons:
        data["application_error_reasons"] = reasons
    return data


def _legacy_error(data: dict) -> tuple[str, str]:
    code = data.get("error_code")
    if not isinstance(code, str) or code not in CANONICAL_ERROR_CODES:
        code = GENERIC_ERROR_CODE
    message = data.get("error_message")
    if not isinstance(message, str):
        return code, GENERIC_ERROR_MESSAGE
    message = " ".join(message.split())
    if (
        not message
        or len(message) > MAX_LEGACY_ERROR_MESSAGE_LENGTH
        or any(ord(char) < 32 and char not in "\t\r\n" for char in message)
        or _UNTRUSTED_ERROR_MESSAGE.search(message)
    ):
        message = GENERIC_ERROR_MESSAGE
    return code, message


def _filtered_error(data: dict, evaluation: dict) -> tuple[str, str] | None:
    blocker = data.get("blocker")
    if blocker == "test" or evaluation.get("decision") == "skip":
        return None
    if data.get("security_incident") or data.get("security_incident_recorded"):
        return "SECURITY_BLOCKED", "Обработка вакансии остановлена из-за небезопасного содержимого"
    if data.get("error_code") or data.get("error_message"):
        return _legacy_error(data)
    if blocker == "mfa":
        return "MFA_REQUIRED", "Сайт запросил дополнительную проверку аккаунта"
    if blocker == "blocked":
        return "SITE_ACCESS_BLOCKED", "Сайт ограничил доступ к вакансии или отклику"
    if blocker == "unknown_form":
        return "APPLICATION_FORM_UNSUPPORTED", "Форма вакансии не поддерживается автоматически"
    if data.get("application_error_reasons"):
        return "APPLICATION_FORM_UNRESOLVED", "Не удалось безопасно заполнить обязательные вопросы анкеты"
    if data.get("cover_letter_error") or data.get("cover_letter_attempts"):
        return "VACANCY_PROCESSING_FAILED", "Не удалось автоматически подготовить сопроводительное письмо"
    return None


def _set_generic_error(data: dict) -> None:
    data["error_code"] = GENERIC_ERROR_CODE
    data["error_message"] = GENERIC_ERROR_MESSAGE


def upgrade() -> None:
    bind = op.get_bind()
    vacancies = sa.table(
        "vacancies",
        sa.column("id", sa.Integer),
        sa.column("state", sa.String),
        sa.column("data", sa.JSON),
    )
    evaluations = sa.table(
        "evaluations",
        sa.column("vacancy_id", sa.Integer),
        sa.column("data", sa.JSON),
    )
    evaluation_data = {
        row.vacancy_id: _json(row.data)
        for row in bind.execute(sa.select(evaluations.c.vacancy_id, evaluations.c.data))
    }
    for row in bind.execute(sa.select(vacancies.c.id, vacancies.c.state, vacancies.c.data)):
        state = str(row.state or "EXTRACTED")
        data = _rename_error_reasons(_json(row.data))
        evaluation = evaluation_data.get(row.id, {})
        if state == "FILTERED_OUT":
            error = _filtered_error(data, evaluation)
            if error is None:
                state = "REJECTED_BY_MODEL"
            else:
                state = "ERROR"
                data["error_code"], data["error_message"] = error
        elif state in {"UNKNOWN", "FAILED", "UNKNOWN_RESULT", "NEEDS_REVIEW", "BLOCKED"}:
            state = "ERROR"
            _set_generic_error(data)
        else:
            state = {
                "DISCOVERED": "EXTRACTED",
                "SKIPPED_TEST": "REJECTED_BY_MODEL",
                "LETTER_GENERATED": "READY_TO_SUBMIT",
                "FILLING_FORM": "SUBMITTING",
            }.get(state, state)
        if state not in CANONICAL_VACANCY_STATES:
            state = "ERROR"
            _set_generic_error(data)
        bind.execute(
            vacancies.update().where(vacancies.c.id == row.id).values(state=state, data=data)
        )

    session_columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
    session_table_columns = [sa.column("id", sa.Integer), sa.column("status", sa.String)]
    if "stop_reason" in session_columns:
        session_table_columns.append(sa.column("stop_reason", sa.String))
    sessions = sa.table("sessions", *session_table_columns)
    bind.execute(
        sessions.update().where(sessions.c.status == "NEEDS_REVIEW").values(status="FAILED")
    )
    waiting_update = {"status": "PAUSED"}
    if "stop_reason" in session_columns:
        waiting_update["stop_reason"] = WAITING_FOR_LOGIN_STOP_REASON
    bind.execute(sessions.update().where(sessions.c.status == "WAITING_FOR_LOGIN").values(**waiting_update))

    plans = sa.table("application_plans", sa.column("id", sa.Integer), sa.column("data", sa.JSON))
    for row in bind.execute(sa.select(plans.c.id, plans.c.data)):
        data = _json(row.data)
        data.pop("unknown_question_policy", None)
        bind.execute(plans.update().where(plans.c.id == row.id).values(data=data))

    for row in bind.execute(sa.select(evaluations.c.vacancy_id, evaluations.c.data)):
        data = _json(row.data)
        if data.get("decision") == "manual_review":
            data["decision"] = "skip"
        data.pop("requires_manual_review", None)
        bind.execute(
            evaluations.update()
            .where(evaluations.c.vacancy_id == row.vacancy_id)
            .values(data=data)
        )


def downgrade() -> None:
    # Status normalization is intentionally irreversible; legacy values are
    # not reintroduced after an upgrade.
    pass
