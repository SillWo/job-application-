from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def test_session_resume_migration_creates_ephemeral_tables(tmp_path: Path):
    path = tmp_path / "empty.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "head")
    db = sqlite3.connect(path)
    try:
        columns = {row[1]: row for row in db.execute("pragma table_info(sessions)")}
        assert "profile_id" not in columns
        assert "session_answers" not in columns
        app_columns = {row[1]: row for row in db.execute("pragma table_info(applications)")}
        assert "candidate_profile_id" not in app_columns
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        assert {"session_resume_snapshots", "resume_preview_tokens", "saved_resume_sources"} <= tables
        snapshot_columns = {row[1] for row in db.execute("pragma table_info(session_resume_snapshots)")}
        assert "private_view" in snapshot_columns
        assert "expires_at" in snapshot_columns
        assert "full_snapshot" in snapshot_columns
        preview_columns = {row[1] for row in db.execute("pragma table_info(resume_preview_tokens)")}
        assert "source_url_encrypted" not in preview_columns
        assert "source_url" in preview_columns
        assert {row[1]: row for row in db.execute("pragma table_info(session_resume_snapshots)")}[
            "private_view"
        ][2].upper() == "TEXT"
        saved_columns = {row[1] for row in db.execute("pragma table_info(saved_resume_sources)")}
        assert {
            "adapter_id", "source_url", "source_url_hash", "resume_id_hash",
            "grammatical_gender", "content_hash", "preview", "status", "checked_at",
            "changed", "error_code",
        } <= saved_columns
    finally:
        db.close()


def test_application_profile_nullable_repair_preserves_populated_sqlite_schema(tmp_path: Path):
    """0034 repairs an old NOT NULL table without dropping its data/constraint."""
    path = tmp_path / "application-repair.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0033")

    db = sqlite3.connect(path)
    try:
        # A production installation can be stamped at 0033 while its
        # applications table still has the pre-0028 constraint.  Rebuild that
        # exact table shape while retaining a real populated row and DDL.
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO candidate_profiles "
            "(id, full_name, job_search_locations, contacts, education, languages, created_at, updated_at) "
            "VALUES (42, 'Fixture', '[]', '{}', '[]', '[]', '2026-01-01', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO vacancies "
            "(id, session_id, source, site, external_id, url, title, state, status_changed_at, data, updated_at) "
            "VALUES (99, NULL, 'hh', 'HH.ru', 'fixture', 'https://hh.test/fixture', 'Fixture', "
            "'EXTRACTED', '2026-01-01', '{}', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO applications "
            "(id, candidate_profile_id, vacancy_id, status, submitted_at) "
            "VALUES (1, 42, 99, 'submitted', NULL)"
        )
        create_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'applications'"
        ).fetchone()[0]
        broken_sql = re.sub(
            r"(candidate_profile_id\s+INTEGER)(\s*,|\s+REFERENCES)",
            r"\1 NOT NULL\2",
            create_sql,
            count=1,
            flags=re.IGNORECASE,
        )
        assert broken_sql != create_sql
        db.execute("ALTER TABLE applications RENAME TO applications_legacy")
        db.execute(broken_sql)
        db.execute(
            "INSERT INTO applications SELECT * FROM applications_legacy"
        )
        db.execute("DROP TABLE applications_legacy")
        db.commit()
        assert db.execute(
            "SELECT \"notnull\" FROM pragma_table_info('applications') "
            "WHERE name = 'candidate_profile_id'"
        ).fetchone()[0] == 1
    finally:
        db.close()

    command.upgrade(config, "head")

    db = sqlite3.connect(path)
    try:
        columns = {row[1]: row for row in db.execute("pragma table_info(applications)")}
        assert "candidate_profile_id" not in columns
        assert db.execute(
            "SELECT vacancy_id, status FROM applications WHERE id = 1"
        ).fetchone() == (99, "submitted")
        indexes = db.execute("pragma index_list(applications)").fetchall()
        assert any(index[2] for index in indexes), "unique application constraint was lost"

        # The repaired schema accepts the session-scoped submission path,
        # which intentionally has no candidate profile.
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO applications "
                "(id, vacancy_id, status, submitted_at) "
                "VALUES (2, 99, 'submitted', NULL)"
            )
        db.commit()
    finally:
        db.close()


def test_saved_source_gender_migration_upgrades_database_at_0032(tmp_path: Path):
    path = tmp_path / "saved-source-0032.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0032")
    db = sqlite3.connect(path)
    try:
        before = {row[1] for row in db.execute("pragma table_info(saved_resume_sources)")}
        assert "grammatical_gender" not in before
    finally:
        db.close()

    command.upgrade(config, "head")
    db = sqlite3.connect(path)
    try:
        columns = {row[1] for row in db.execute("pragma table_info(saved_resume_sources)")}
        assert "grammatical_gender" in columns
    finally:
        db.close()


def test_session_answers_is_repaired_for_database_already_at_0030(tmp_path: Path):
    path = tmp_path / "legacy.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0030")

    db = sqlite3.connect(path)
    try:
        db.execute("alter table sessions drop column session_answers")
        db.commit()
        assert "session_answers" not in {row[1] for row in db.execute("pragma table_info(sessions)")}
    finally:
        db.close()

    command.upgrade(config, "head")
    db = sqlite3.connect(path)
    try:
        columns = {row[1]: row for row in db.execute("pragma table_info(sessions)")}
        assert "session_answers" not in columns
    finally:
        db.close()


def test_session_answers_survives_repair_migration_downgrade(tmp_path: Path):
    path = tmp_path / "downgrade.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "head")
    command.downgrade(config, "0030")

    db = sqlite3.connect(path)
    try:
        assert "session_answers" in {row[1] for row in db.execute("pragma table_info(sessions)")}
    finally:
        db.close()


def test_resume_storage_migration_normalizes_existing_unconfirmed_outcome(tmp_path: Path):
    path = tmp_path / "resume-storage-existing.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0034")
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into vacancies "
            "(session_id, source, site, external_id, url, title, state, status_changed_at, data, updated_at) "
            "values (NULL, 'hh', '', 'unconfirmed', 'https://example.test/u', 'U', 'ERROR', "
            "CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)",
            (json.dumps({"error_code": "SUBMISSION_UNCONFIRMED", "error_message": "[удалено]"}),),
        )
        db.commit()
        db.execute(
            "insert into sessions (adapter_id, status, counters) values ('hh', 'CREATED', '{}')"
        )
        session_id = db.execute("select id from sessions order by id desc limit 1").fetchone()[0]
        # Reproduce the deployed drift: the old database declared sealed
        # values as JSON while storing raw dpapi text.
        for table in ("session_resume_snapshots", "resume_preview_tokens"):
            create_sql = db.execute(
                "select sql from sqlite_master where type='table' and name = ?", (table,)
            ).fetchone()[0]
            db.execute(f"alter table {table} rename to {table}_legacy")
            db.execute(create_sql.replace("private_view TEXT", "private_view JSON"))
            db.execute(f"insert into {table} select * from {table}_legacy")
            db.execute(f"drop table {table}_legacy")
        db.execute(
            "insert into session_resume_snapshots "
            "(session_id, source_site, source_resume_id, source_url_hash, content_hash, imported_at, "
                "snapshot, professional_view, full_snapshot, private_view, expires_at) "
                "values (?, 'hh', 'legacy', ?, ?, CURRENT_TIMESTAMP, '{}', '{}', NULL, 'dpapi:legacy', NULL)",
            (session_id, "a" * 64, "b" * 64),
        )
        db.execute(
            "insert into resume_preview_tokens "
            "(token_hash, adapter_id, snapshot, professional_view, full_snapshot, private_view, "
            "source_url, created_at, expires_at, consumed_at) "
            "values ('c' || printf('%063d', 1), 'hh', '{}', '{}', NULL, 'dpapi:legacy-preview', "
            "'https://hh.ru/resume/legacy', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)"
        )
        db.commit()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        state, data = db.execute(
            "select state, data from vacancies where external_id = 'unconfirmed'"
        ).fetchone()
        assert state == "UNCONFIRMED"
        assert json.loads(data)["error_message"] == (
            "Площадка не подтвердила результат отправки; отклик мог быть отправлен"
        )
        snapshot_columns = {row[1]: row for row in db.execute("pragma table_info(session_resume_snapshots)")}
        preview_columns = {row[1]: row for row in db.execute("pragma table_info(resume_preview_tokens)")}
        assert snapshot_columns["private_view"][2].upper() == "TEXT"
        assert preview_columns["private_view"][2].upper() == "TEXT"
        assert "full_snapshot" in snapshot_columns and "source_url" in preview_columns
        assert "source_url_encrypted" not in preview_columns
        assert db.execute("select private_view from session_resume_snapshots").fetchone()[0] == "dpapi:legacy"
        assert db.execute("select private_view from resume_preview_tokens").fetchone()[0] == "dpapi:legacy-preview"
