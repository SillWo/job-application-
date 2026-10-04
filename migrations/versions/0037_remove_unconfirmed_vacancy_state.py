"""Normalize the retired ambiguous-submission vacancy state to ERROR."""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_ERROR_CODE = "SUBMISSION_UNCONFIRMED"
_DEFAULT_MESSAGE = "Не удалось подтвердить отправку отклика после нескольких попыток"
_PENDING_STATES = (
    "EXTRACTED", "EVALUATING", "READY_TO_SUBMIT", "READY_TO_REPORT", "SUBMITTING",
)
_SESSION_OUTCOMES = {
    "STOPPED": (
        "SESSION_STOPPED", "Вакансия не обработана: сессия остановлена пользователем"
    ),
    "FAILED": (
        "SESSION_FAILED", "Вакансия не обработана: сессия завершилась с ошибкой"
    ),
    "COMPLETED": (
        "VACANCY_PROCESSING_FAILED", "Вакансия не обработана до завершения сессии"
    ),
}


def _json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("vacancies"):
        return
    columns = {column["name"] for column in inspector.get_columns("vacancies")}
    if not {"id", "state", "data"} <= columns:
        return

    vacancies = sa.table(
        "vacancies",
        sa.column("id", sa.Integer),
        sa.column("state", sa.String),
        sa.column("data", sa.JSON),
    )
    rows = bind.execute(
        sa.select(vacancies.c.id, vacancies.c.data).where(
            vacancies.c.state == "UNCONFIRMED"
        )
    ).all()
    for row in rows:
        data = _json_dict(row.data)
        data["error_code"] = _ERROR_CODE
        message = data.get("error_message")
        if not isinstance(message, str) or not message.strip() or message.strip() == "[удалено]":
            data["error_message"] = _DEFAULT_MESSAGE
        bind.execute(
            vacancies.update().where(vacancies.c.id == row.id).values(
                state="ERROR", data=data
            )
        )

    _terminalize_historical_processing_vacancies(bind)


def _terminalize_historical_processing_vacancies(bind: sa.Connection) -> None:
    """Close processing rows belonging to sessions already at a terminal status."""
    inspector = sa.inspect(bind)
    if not inspector.has_table("sessions"):
        return
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    if not {"id", "status", "counters"} <= session_columns:
        return
    vacancy_columns = {column["name"] for column in inspector.get_columns("vacancies")}
    if not {"id", "session_id", "state", "data"} <= vacancy_columns:
        return

    sessions = sa.table(
        "sessions",
        sa.column("id", sa.Integer),
        sa.column("status", sa.String),
        sa.column("counters", sa.JSON),
    )
    vacancies = sa.table(
        "vacancies",
        sa.column("id", sa.Integer),
        sa.column("session_id", sa.Integer),
        sa.column("state", sa.String),
        sa.column("data", sa.JSON),
    )
    for session in bind.execute(
        sa.select(sessions.c.id, sessions.c.status, sessions.c.counters).where(
            sessions.c.status.in_(tuple(_SESSION_OUTCOMES))
        )
    ).all():
        error_code, error_message = _SESSION_OUTCOMES[session.status]
        rows = bind.execute(
            sa.select(vacancies.c.id, vacancies.c.data).where(
                vacancies.c.session_id == session.id,
                vacancies.c.state.in_(_PENDING_STATES),
            )
        ).all()
        if not rows:
            continue
        for row in rows:
            data = _json_dict(row.data)
            data["error_code"] = error_code
            data["error_message"] = error_message
            bind.execute(
                vacancies.update().where(vacancies.c.id == row.id).values(
                    state="ERROR", data=data
                )
            )
        counters = _json_dict(session.counters)
        try:
            previous_errors = max(0, int(counters.get("errors", 0)))
        except (TypeError, ValueError):
            previous_errors = 0
        counters["errors"] = previous_errors + len(rows)
        bind.execute(
            sessions.update().where(sessions.c.id == session.id).values(counters=counters)
        )


def downgrade() -> None:
    # Forward-only normalization: the retired state must never be recreated.
    return
