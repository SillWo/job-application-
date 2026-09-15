from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _config(path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _upgrade(path: Path, revision: str) -> None:
    command.upgrade(_config(path), revision)


def _insert_profile_session(db: sqlite3.Connection, *, status: str = "STOPPED") -> int:
    db.execute(
        "insert into candidate_profiles "
        "(full_name, job_search_locations, contacts, education, languages, created_at, updated_at) "
        "values ('Legacy', '[]', '{}', '[]', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    profile_id = db.execute("select last_insert_rowid()").fetchone()[0]
    db.execute(
        "insert into sessions (profile_id, adapter_id, status, counters) values (?, 'hh', ?, '{}')",
        (profile_id, status),
    )
    return db.execute("select last_insert_rowid()").fetchone()[0]


def test_terminal_legacy_cleanup_detaches_history_and_removes_schema(tmp_path: Path):
    path = tmp_path / "legacy-terminal.db"
    _upgrade(path, "0035")
    with sqlite3.connect(path) as db:
        session_id = _insert_profile_session(db)
        db.execute(
            "insert into vacancies (session_id, source, site, external_id, url, title, state, "
            "status_changed_at, data, updated_at) values (?, 'hh', '', 'v1', 'u', 'V', "
            "'SUBMITTED', CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)",
            (session_id,),
        )
        vacancy_id = db.execute("select last_insert_rowid()").fetchone()[0]
        db.execute(
            "insert into browser_events (session_id, event_type, message, data, created_at) "
            "values (?, 'x', 'x', '{}', CURRENT_TIMESTAMP)", (session_id,)
        )
        db.execute(
            "insert into session_resume_snapshots "
            "(session_id, source_site, source_resume_id, source_url_hash, content_hash, imported_at, "
            "snapshot, professional_view, private_view) values (?, 'hh', 'r', ?, ?, CURRENT_TIMESTAMP, "
            "'{}', '{}', 'dpapi:x')",
            (session_id, "a" * 64, "b" * 64),
        )
        db.execute(
            "insert into notifications (source_type, source_id, target_path, kind, title, message, created_at) "
            "values ('session', ?, '/sessions', 'x', 'x', 'x', CURRENT_TIMESTAMP)", (str(session_id),)
        )
        db.execute(
            "insert into notifications (source_type, source_id, target_path, kind, title, message, created_at) "
            "values ('vacancy', ?, '/vacancies', 'x', 'x', 'x', CURRENT_TIMESTAMP)", (str(vacancy_id),)
        )
        db.execute(
            "insert into applications (candidate_profile_id, vacancy_id, status) values (NULL, ?, 'submitted')",
            (vacancy_id,),
        )
        db.commit()

    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        assert not tables & {"candidate_profiles", "resumes", "profile_memory", "session_questions"}
        assert db.execute("select id from sessions").fetchall() == []
        assert db.execute("select session_id from vacancies where id = ?", (vacancy_id,)).fetchone() == (None,)
        assert db.execute("select session_id from browser_events").fetchall() == []
        assert db.execute("select source_type from notifications").fetchall() == [("vacancy",)]
        assert db.execute("select vacancy_id from applications").fetchall() == [(vacancy_id,)]
        assert "profile_id" not in {row[1] for row in db.execute("pragma table_info(sessions)")}
        assert "candidate_profile_id" not in {row[1] for row in db.execute("pragma table_info(applications)")}


def test_profileless_active_session_survives_and_vacancy_application_is_unique(tmp_path: Path):
    path = tmp_path / "profileless.db"
    _upgrade(path, "0035")
    with sqlite3.connect(path) as db:
        db.execute("insert into sessions (adapter_id, status, counters) values ('hh', 'RUNNING', '{}')")
        db.execute(
            "insert into vacancies (session_id, source, site, external_id, url, title, state, "
            "status_changed_at, data, updated_at) values (1, 'hh', '', 'v', 'u', 'V', 'EXTRACTED', "
            "CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)"
        )
        db.execute("insert into applications (vacancy_id, status) values (1, 'submitted')")
        db.commit()
    _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("select id from sessions").fetchall() == [(1,)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("insert into applications (vacancy_id, status) values (1, 'submitted')")


def test_nonterminal_legacy_guard_aborts_before_mutation(tmp_path: Path):
    path = tmp_path / "unsafe-session.db"
    _upgrade(path, "0035")
    with sqlite3.connect(path) as db:
        session_id = _insert_profile_session(db, status="running")
        db.commit()
    with pytest.raises(RuntimeError, match="non-terminal"):
        _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("select id from sessions").fetchall() == [(session_id,)]
        assert "candidate_profiles" in {row[0] for row in db.execute("select name from sqlite_master where type='table'")}


def test_duplicate_application_guard_aborts_before_mutation(tmp_path: Path):
    path = tmp_path / "duplicate-application.db"
    _upgrade(path, "0035")
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into vacancies (source, site, external_id, url, title, state, status_changed_at, data, updated_at) "
            "values ('hh', '', 'v', 'u', 'V', 'EXTRACTED', CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)"
        )
        db.execute("insert into applications (candidate_profile_id, vacancy_id, status) values (NULL, 1, 'a')")
        db.execute("insert into applications (candidate_profile_id, vacancy_id, status) values (NULL, 1, 'b')")
        db.commit()
    with pytest.raises(RuntimeError, match="duplicate vacancy_id"):
        _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from applications").fetchone() == (2,)
        assert "candidate_profile_id" in {row[1] for row in db.execute("pragma table_info(applications)")}


def test_encrypted_public_url_without_plaintext_guard_aborts(tmp_path: Path):
    path = tmp_path / "encrypted-url.db"
    _upgrade(path, "0035")
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into saved_resume_sources "
            "(adapter_id, source_url, source_url_encrypted, source_url_hash, resume_id_hash, "
            "content_hash, preview, status, changed) values "
            "('hh', NULL, 'dpapi:legacy', ?, ?, ?, '{}', 'valid', 0)",
            ("a" * 64, "b" * 64, "c" * 64),
        )
        db.commit()
    with pytest.raises(RuntimeError, match="canonical source_url"):
        _upgrade(path, "head")
    with sqlite3.connect(path) as db:
        assert "source_url_encrypted" in {row[1] for row in db.execute("pragma table_info(saved_resume_sources)")}


def test_downgrade_restores_empty_legacy_shape(tmp_path: Path):
    path = tmp_path / "downgrade.db"
    _upgrade(path, "head")
    config = _config(path)
    command.downgrade(config, "0035")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        assert {"candidate_profiles", "resumes", "profile_memory", "session_questions"} <= tables
        session_columns = {row[1] for row in db.execute("pragma table_info(sessions)")}
        assert {"profile_id", "session_answers", "questions_collected_at"} <= session_columns
        application_columns = {row[1] for row in db.execute("pragma table_info(applications)")}
        assert "candidate_profile_id" in application_columns
        assert {"source_url_encrypted"} <= {
            row[1] for row in db.execute("pragma table_info(saved_resume_sources)")
        }
        assert {"source_url_encrypted"} <= {
            row[1] for row in db.execute("pragma table_info(resume_preview_tokens)")
        }
        foreign_keys = db.execute("pragma foreign_key_list(sessions)").fetchall()
        assert any(row[2] == "candidate_profiles" and row[3] == "profile_id" for row in foreign_keys)
