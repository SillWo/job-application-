from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

VACANCY_PROJECTION_COLUMNS = {
    "search_text",
    "title_sort",
    "site_sort",
    "error_code",
    "error_message",
}
EVALUATION_PROJECTION_COLUMNS = {
    "total_score",
    "tasks",
    "skills",
    "experience_depth",
    "role_match",
    "industry",
    "special_requirements",
    "decision",
    "confidence",
    "category",
}
VACANCY_PROJECTION_INDEXES = {
    "ix_vacancies_projection_search",
    "ix_vacancies_projection_title",
    "ix_vacancies_projection_site",
    "ix_vacancies_projection_state",
    "ix_vacancies_projection_date",
    "ix_vacancies_projection_title_asc",
    "ix_vacancies_projection_site_asc",
    "ix_vacancies_projection_state_asc",
    "ix_vacancies_projection_date_asc",
}
EVALUATION_PROJECTION_INDEXES = {
    f"ix_evaluations_projection_{field}{suffix}"
    for field in (
        "total",
        "tasks",
        "skills",
        "experience_depth",
        "role_match",
        "industry",
        "special_requirements",
    )
    for suffix in ("", "_asc")
}


def _config(path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"pragma table_info({table})")}


def _indexes(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"pragma index_list({table})")}


def _history(db: sqlite3.Connection) -> tuple:
    vacancy = db.execute(
        "select data from vacancies where external_id = 'projection-history'"
    ).fetchone()[0]
    evaluation = db.execute(
        "select data from evaluations where vacancy_id = "
        "(select id from vacancies where external_id = 'projection-history')"
    ).fetchone()[0]
    snapshot = db.execute(
        "select source_url_hash, content_hash, snapshot, professional_view, "
        "full_snapshot, private_view from session_resume_snapshots"
    ).fetchone()
    return vacancy, evaluation, snapshot


def _assert_projection_absent(db: sqlite3.Connection) -> None:
    assert not VACANCY_PROJECTION_COLUMNS & _columns(db, "vacancies")
    assert not EVALUATION_PROJECTION_COLUMNS & _columns(db, "evaluations")
    assert not {
        name for name in _indexes(db, "vacancies") if name.startswith("ix_vacancies_projection_")
    }
    assert not {
        name
        for name in _indexes(db, "evaluations")
        if name.startswith("ix_evaluations_projection_")
    }


def _assert_projection_present(db: sqlite3.Connection) -> None:
    assert _columns(db, "vacancies") >= VACANCY_PROJECTION_COLUMNS
    assert _columns(db, "evaluations") >= EVALUATION_PROJECTION_COLUMNS
    assert _indexes(db, "vacancies") >= VACANCY_PROJECTION_INDEXES
    assert _indexes(db, "evaluations") >= EVALUATION_PROJECTION_INDEXES


def test_projection_upgrade_and_rollback_preserve_json_and_resume_hashes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projection-history.db"
    config = _config(path)
    command.upgrade(config, "0038")

    vacancy_data = json.dumps(
        {
            "error_code": "FIXTURE_CODE",
            "error_message": "fixture message",
            "nested": {"untouched": [3, 2, 1]},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    evaluation_data = json.dumps(
        {
            "score": 73.5,
            "decision": "apply",
            "confidence": 0.91,
            "category": "strong",
            "score_breakdown": [
                {"key": "tasks", "points": 12.5},
                {"key": "skills", "points": 11},
            ],
            "nested": {"must_remain": True},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    snapshot_json = json.dumps({"resume": {"title": "Fixture"}}, separators=(",", ":"))
    professional_json = json.dumps({"skills": ["SQL"]}, separators=(",", ":"))
    full_snapshot_json = json.dumps({"identity": {"name": "Fixture"}}, separators=(",", ":"))
    source_url_hash = "a" * 64
    content_hash = "b" * 64

    with sqlite3.connect(path) as db:
        _assert_projection_absent(db)
        db.execute(
            "insert into sessions (adapter_id, status, counters) "
            "values ('hirehi', 'COMPLETED', '{}')"
        )
        session_id = db.execute("select id from sessions").fetchone()[0]
        db.execute(
            "insert into vacancies "
            "(session_id, source, site, external_id, url, title, company, state, "
            "status_changed_at, data, updated_at) values "
            "(?, 'hirehi', 'HireHi', 'projection-history', "
            "'https://example.test/vacancy', 'Data Engineer', 'Fixture Co', "
            "'REPORTED', '2026-09-20 12:00:00', ?, '2026-09-20 12:00:00')",
            (session_id, vacancy_data),
        )
        vacancy_id = db.execute("select id from vacancies").fetchone()[0]
        db.execute(
            "insert into evaluations (vacancy_id, data) values (?, ?)",
            (vacancy_id, evaluation_data),
        )
        db.execute(
            "insert into session_resume_snapshots "
            "(session_id, source_site, source_resume_id, source_url_hash, content_hash, "
            "imported_at, snapshot, professional_view, full_snapshot, private_view) "
            "values (?, 'hirehi', 'fixture-id', ?, ?, '2026-09-20 11:00:00', ?, ?, ?, ?)",
            (
                session_id,
                source_url_hash,
                content_hash,
                snapshot_json,
                professional_json,
                full_snapshot_json,
                "sealed-fixture",
            ),
        )
        db.commit()
        before = _history(db)

    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        _assert_projection_present(db)
        assert _history(db) == before
        assert db.execute(
            "select search_text, title_sort, site_sort, error_code, error_message "
            "from vacancies where external_id = 'projection-history'"
        ).fetchone() == (
            "projection-history data engineer fixture co",
            "data engineer",
            "hirehi",
            "FIXTURE_CODE",
            "fixture message",
        )
        assert db.execute(
            "select total_score, tasks, skills, decision, confidence, category "
            "from evaluations where vacancy_id = ?",
            (vacancy_id,),
        ).fetchone() == (73.5, 12.5, 11.0, "apply", 0.91, "strong")

    command.downgrade(config, "0038")
    with sqlite3.connect(path) as db:
        _assert_projection_absent(db)
        assert _history(db) == before

    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        _assert_projection_present(db)
        assert _history(db) == before
