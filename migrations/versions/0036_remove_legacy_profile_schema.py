"""Remove the profile-memory schema after detaching terminal legacy sessions.

The migration is intentionally self-contained.  It reflects the database and
does not import the runtime ORM, because the latter no longer describes the
legacy tables being removed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_TERMINAL = ("COMPLETED", "STOPPED", "FAILED")


def _inspector(bind: sa.Connection) -> sa.Inspector:
    return sa.inspect(bind)


def _has_table(bind: sa.Connection, table: str) -> bool:
    return _inspector(bind).has_table(table)


def _columns(bind: sa.Connection, table: str) -> dict[str, dict]:
    if not _has_table(bind, table):
        return {}
    return {item["name"]: item for item in _inspector(bind).get_columns(table)}


def _table(bind: sa.Connection, name: str, columns: dict[str, dict]) -> sa.Table:
    return sa.Table(
        name,
        sa.MetaData(),
        *(sa.Column(column, info.get("type", sa.Text())) for column, info in columns.items()),
    )


def _assert_safe_to_cleanup(bind: sa.Connection) -> None:
    sessions = _columns(bind, "sessions")
    if {"id", "profile_id", "status"} <= set(sessions):
        session = _table(bind, "sessions", {
            "id": sessions["id"],
            "profile_id": sessions["profile_id"],
            "status": sessions["status"],
        })
        unsafe = bind.execute(
            sa.select(session.c.id, session.c.status)
            .where(
                session.c.profile_id.is_not(None),
                sa.or_(
                    session.c.status.is_(None),
                    sa.func.upper(session.c.status).not_in(_TERMINAL),
                ),
            )
            .limit(1)
        ).first()
        if unsafe is not None:
            raise RuntimeError(
                "0036 aborted: legacy profile session "
                f"{unsafe.id} has non-terminal status {unsafe.status!r}"
            )

    applications = _columns(bind, "applications")
    if {"vacancy_id"} <= set(applications):
        application = _table(bind, "applications", {"vacancy_id": applications["vacancy_id"]})
        duplicate = bind.execute(
            sa.select(application.c.vacancy_id, sa.func.count().label("count"))
            .group_by(application.c.vacancy_id)
            .having(sa.func.count() > 1)
            .limit(1)
        ).first()
        if duplicate is not None:
            raise RuntimeError(
                "0036 aborted: applications contains duplicate vacancy_id "
                f"{duplicate.vacancy_id!r} ({duplicate.count} rows)"
            )

    for table_name in ("saved_resume_sources", "resume_preview_tokens"):
        columns = _columns(bind, table_name)
        if not {"source_url", "source_url_encrypted"} <= set(columns):
            continue
        table = _table(bind, table_name, {
            "source_url": columns["source_url"],
            "source_url_encrypted": columns["source_url_encrypted"],
        })
        orphan = bind.execute(
            sa.select(sa.literal(1)).where(
                table.c.source_url_encrypted.is_not(None),
                sa.func.trim(table.c.source_url_encrypted) != "",
                sa.or_(
                    table.c.source_url.is_(None),
                    sa.func.trim(table.c.source_url) == "",
                ),
            ).limit(1)
        ).first()
        if orphan is not None:
            raise RuntimeError(
                f"0036 aborted: {table_name} contains an encrypted public URL "
                "without its canonical source_url"
            )


def _delete_terminal_legacy_sessions(bind: sa.Connection) -> None:
    columns = _columns(bind, "sessions")
    if not {"id", "profile_id", "status"} <= set(columns):
        return
    session = _table(bind, "sessions", {
        "id": columns["id"],
        "profile_id": columns["profile_id"],
        "status": columns["status"],
    })
    ids = [row.id for row in bind.execute(
        sa.select(session.c.id).where(
            session.c.profile_id.is_not(None),
            sa.func.upper(session.c.status).in_(_TERMINAL),
        )
    )]
    if not ids:
        return

    vacancies = _columns(bind, "vacancies")
    if "session_id" in vacancies:
        vacancy = _table(bind, "vacancies", {"session_id": vacancies["session_id"]})
        bind.execute(
            vacancy.update().where(vacancy.c.session_id.in_(ids)).values(session_id=None)
        )
    for table_name in ("browser_events", "session_resume_snapshots", "notifications"):
        table_columns = _columns(bind, table_name)
        if table_name == "notifications":
            if {"source_type", "source_id"} <= set(table_columns):
                notification = _table(bind, "notifications", {
                    "source_type": table_columns["source_type"],
                    "source_id": table_columns["source_id"],
                })
                bind.execute(
                    notification.delete().where(
                        notification.c.source_type == "session",
                        notification.c.source_id.in_([str(item) for item in ids]),
                    )
                )
            continue
        if "session_id" in table_columns:
            related = _table(bind, table_name, {"session_id": table_columns["session_id"]})
            bind.execute(related.delete().where(related.c.session_id.in_(ids)))
    bind.execute(session.delete().where(session.c.id.in_(ids)))


def _copy_column(column: sa.Column, *, foreign_key: str | None = None) -> sa.Column:
    args = []
    if foreign_key:
        args.append(sa.ForeignKey(foreign_key))
    return sa.Column(
        column.name,
        column.type,
        *args,
        nullable=column.nullable,
        primary_key=column.primary_key,
        server_default=column.server_default,
    )


def _rebuild_applications(bind: sa.Connection, *, legacy: bool) -> None:
    if not _has_table(bind, "applications"):
        return
    old = sa.Table("applications", sa.MetaData(), autoload_with=bind)
    columns = []
    if legacy:
        columns.append(sa.Column(
            "candidate_profile_id", sa.Integer(), sa.ForeignKey("candidate_profiles.id"), nullable=True
        ))
    for column in old.columns:
        if column.name != "candidate_profile_id":
            fk = "vacancies.id" if column.name == "vacancy_id" else None
            columns.append(_copy_column(column, foreign_key=fk))
    columns.append(sa.UniqueConstraint(
        *("candidate_profile_id", "vacancy_id") if legacy else ("vacancy_id",),
        name=("uq_applications_candidate_profile_id_vacancy_id" if legacy else "uq_applications_vacancy_id"),
    ))
    metadata = sa.MetaData()
    sa.Table("vacancies", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    if legacy:
        sa.Table("candidate_profiles", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    replacement = sa.Table("_applications_0036", metadata, *columns)
    replacement.create(bind)
    names = [column.name for column in replacement.columns]
    source_names = [name for name in names if name in old.c]
    if legacy:
        values = [old.c[name] for name in source_names]
    else:
        values = [old.c[name] for name in source_names]
    bind.execute(replacement.insert().from_select(source_names, sa.select(*values)))
    old.drop(bind)
    op.rename_table("_applications_0036", "applications")


def _drop_session_columns(bind: sa.Connection) -> None:
    columns = _columns(bind, "sessions")
    names = [name for name in ("profile_id", "session_answers", "questions_collected_at") if name in columns]
    if not names:
        return
    with op.batch_alter_table("sessions") as batch:
        for name in names:
            batch.drop_column(name)


def _drop_columns(bind: sa.Connection, table: str, names: tuple[str, ...]) -> None:
    columns = _columns(bind, table)
    names = tuple(name for name in names if name in columns)
    if not names:
        return
    with op.batch_alter_table(table) as batch:
        for name in names:
            batch.drop_column(name)


def _create_legacy_tables(bind: sa.Connection) -> None:
    metadata = sa.MetaData()
    sa.Table("sessions", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("vacancies", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("candidate_profiles", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    if not _has_table(bind, "candidate_profiles"):
        sa.Table(
            "candidate_profiles", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("full_name", sa.String(255)), sa.Column("gender", sa.String(10)),
            sa.Column("residence", sa.String(255)),
            sa.Column("job_search_locations", sa.JSON, nullable=False),
            sa.Column("contacts", sa.JSON, nullable=False),
            sa.Column("education", sa.JSON, nullable=False),
            sa.Column("languages", sa.JSON, nullable=False),
            sa.Column("driver_license", sa.Boolean),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            extend_existing=True,
        ).create(bind)
    if not _has_table(bind, "resumes"):
        sa.Table(
            "resumes", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("profile_id", sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("desired_title", sa.String(255)), sa.Column("desired_salary", sa.String(255)),
            sa.Column("employment_types", sa.JSON, nullable=False), sa.Column("work_formats", sa.JSON, nullable=False),
            sa.Column("business_trips", sa.Boolean), sa.Column("experiences", sa.JSON, nullable=False),
            sa.Column("skills", sa.JSON, nullable=False), sa.Column("about", sa.Text, nullable=False),
            sa.Column("selected_for_matching", sa.Boolean, nullable=False),
            sa.Column("original_filename", sa.String(255)), sa.Column("original_path", sa.String(500)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            extend_existing=True,
        ).create(bind)
    if not _has_table(bind, "profile_memory"):
        sa.Table(
            "profile_memory", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("profile_id", sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False),
            sa.Column("memory_key", sa.String(64), nullable=False), sa.Column("question", sa.Text, nullable=False),
            sa.Column("answer", sa.Text, nullable=False), sa.Column("context", sa.JSON, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("profile_id", "memory_key"),
            extend_existing=True,
        ).create(bind)
    if not _has_table(bind, "session_questions"):
        sa.Table(
            "session_questions", metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("session_id", sa.Integer, sa.ForeignKey("sessions.id"), nullable=False),
            sa.Column("profile_id", sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False),
            sa.Column("vacancy_id", sa.Integer, sa.ForeignKey("vacancies.id")),
            sa.Column("memory_key", sa.String(64), nullable=False), sa.Column("question", sa.Text, nullable=False),
            sa.Column("reason", sa.Text, nullable=False), sa.Column("options", sa.JSON, nullable=False),
            sa.Column("context", sa.JSON, nullable=False), sa.Column("status", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("answered_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("session_id", "memory_key"),
            extend_existing=True,
        ).create(bind)


def upgrade() -> None:
    bind = op.get_bind()
    _assert_safe_to_cleanup(bind)
    _delete_terminal_legacy_sessions(bind)
    _drop_columns(bind, "saved_resume_sources", ("source_url_encrypted",))
    _drop_columns(bind, "resume_preview_tokens", ("source_url_encrypted",))
    if _has_table(bind, "applications"):
        _rebuild_applications(bind, legacy=False)
    _drop_session_columns(bind)
    for table in ("session_questions", "profile_memory", "resumes", "candidate_profiles"):
        if _has_table(bind, table):
            op.drop_table(table)


def downgrade() -> None:
    bind = op.get_bind()
    _create_legacy_tables(bind)
    if _has_table(bind, "applications"):
        _rebuild_applications(bind, legacy=True)
    columns = _columns(bind, "sessions")
    with op.batch_alter_table("sessions") as batch:
        if "profile_id" not in columns:
            batch.add_column(
                sa.Column(
                    "profile_id",
                    sa.Integer(),
                    sa.ForeignKey("candidate_profiles.id", name="fk_sessions_profile_id_candidate_profiles"),
                    nullable=True,
                )
            )
        if "session_answers" not in columns:
            batch.add_column(sa.Column("session_answers", sa.JSON(), nullable=False, server_default="{}"))
        if "questions_collected_at" not in columns:
            batch.add_column(sa.Column("questions_collected_at", sa.DateTime(timezone=True), nullable=True))
    for table in ("saved_resume_sources", "resume_preview_tokens"):
        if _has_table(bind, table) and "source_url_encrypted" not in _columns(bind, table):
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("source_url_encrypted", sa.Text(), nullable=True))
