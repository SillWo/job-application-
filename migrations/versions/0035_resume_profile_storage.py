"""Expose public saved resume URLs and retain full normalized snapshots.

This revision deliberately uses reflected table shapes instead of importing
the current ORM metadata.  It therefore repairs databases that were created
by an older binary (including SQLite installations with JSON-typed sealed
columns) without making the migration dependent on today's model classes.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def _columns(bind: sa.Connection, table: str) -> dict[str, dict]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table)}


def _alter_private_view_to_text(bind: sa.Connection, table: str) -> None:
    columns = _columns(bind, table)
    column = columns.get("private_view")
    if column is None:
        return
    # Some deployed databases were created while this field was JSON, while
    # the runtime contract has always been a sealed text envelope.
    type_name = type(column["type"]).__name__.casefold()
    if "json" not in type_name:
        return
    with op.batch_alter_table(table, schema=None) as batch:
        batch.alter_column(
            "private_view",
            existing_type=sa.JSON(),
            type_=sa.Text(),
            existing_nullable=bool(column.get("nullable", True)),
        )


def upgrade() -> None:
    bind = op.get_bind()

    # A submission whose result cannot be confirmed is materially different
    # from a processing error: the response may already have reached the
    # employer.  Normalize only the exact legacy marker and retain every other
    # error state unchanged.
    vacancy_columns = _columns(bind, "vacancies")
    if vacancy_columns and {"id", "state", "data"} <= set(vacancy_columns):
        vacancies = sa.table(
            "vacancies",
            sa.column("id", sa.Integer),
            sa.column("state", sa.String),
            sa.column("data", sa.JSON),
        )
        neutral_message = "Площадка не подтвердила результат отправки; отклик мог быть отправлен"
        for row in bind.execute(sa.select(vacancies.c.id, vacancies.c.state, vacancies.c.data)):
            if row.state != "ERROR":
                continue
            if isinstance(row.data, dict):
                data = dict(row.data)
            elif isinstance(row.data, str):
                try:
                    parsed = json.loads(row.data)
                except (TypeError, ValueError):
                    parsed = {}
                data = dict(parsed) if isinstance(parsed, dict) else {}
            else:
                data = {}
            if data.get("error_code") != "SUBMISSION_UNCONFIRMED":
                continue
            message = data.get("error_message")
            if not isinstance(message, str) or not message.strip() or message.strip() == "[удалено]":
                data["error_message"] = neutral_message
            bind.execute(
                vacancies.update().where(vacancies.c.id == row.id).values(
                    state="UNCONFIRMED", data=data
                )
            )

    saved = _columns(bind, "saved_resume_sources")
    if saved and (
        "source_url" not in saved
        or not bool(saved.get("source_url_encrypted", {}).get("nullable", True))
    ):
        with op.batch_alter_table("saved_resume_sources", schema=None) as batch:
            if "source_url" not in saved:
                batch.add_column(sa.Column("source_url", sa.Text(), nullable=True))
            if "source_url_encrypted" in saved and not bool(saved["source_url_encrypted"].get("nullable", True)):
                batch.alter_column(
                    "source_url_encrypted",
                    existing_type=sa.Text(),
                    nullable=True,
                    existing_nullable=False,
                )

    for table in ("session_resume_snapshots", "resume_preview_tokens"):
        columns = _columns(bind, table)
        if columns and "full_snapshot" not in columns:
            with op.batch_alter_table(table, schema=None) as batch:
                batch.add_column(sa.Column("full_snapshot", sa.JSON(), nullable=True))
        if table == "resume_preview_tokens" and columns and "source_url" not in columns:
            with op.batch_alter_table(table, schema=None) as batch:
                batch.add_column(sa.Column("source_url", sa.Text(), nullable=True))
        _alter_private_view_to_text(bind, table)

    # 0020 intentionally had a no-op upgrade, so a few installations still
    # carry the obsolete column from the original schema.  Remove it here in
    # a deterministic, idempotent repair.
    if "viewed_limit" in _columns(bind, "sessions"):
        with op.batch_alter_table("sessions", schema=None) as batch:
            batch.drop_column("viewed_limit")


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("session_resume_snapshots", "resume_preview_tokens"):
        if "full_snapshot" in _columns(bind, table):
            with op.batch_alter_table(table, schema=None) as batch:
                batch.drop_column("full_snapshot")
        if table == "resume_preview_tokens" and "source_url" in _columns(bind, table):
            with op.batch_alter_table(table, schema=None) as batch:
                batch.drop_column("source_url")
    if "source_url" in _columns(bind, "saved_resume_sources"):
        with op.batch_alter_table("saved_resume_sources", schema=None) as batch:
            batch.drop_column("source_url")
