from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config


def config(path):
    result = Config("alembic.ini")
    result.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return result


def test_pipeline_migration_empty_historical_and_downgrade(tmp_path):
    path = tmp_path / "pipeline-migration.db"
    cfg = config(path)
    command.upgrade(cfg, "0041")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO sessions (id, adapter_id, status, counters, desired_job_description, recovery) "
            "VALUES (991, 'hh', 'COMPLETED', '{}', '', '{}')"
        )
        db.execute(
            "INSERT INTO vacancies (id, session_id, source, site, external_id, url, title, state, "
            "status_changed_at, data, search_text, title_sort, site_sort, updated_at) "
            "VALUES (992, 991, 'hh', 'HH.ru', 'historical', 'https://example.test/historical', "
            "'Historical', 'SUBMITTED', CURRENT_TIMESTAMP, '{}', 'historical', 'historical', "
            "'hh.ru', CURRENT_TIMESTAMP)"
        )
        db.commit()

    command.upgrade(cfg, "0042")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"pipeline_items", "pipeline_checkpoints", "pipeline_model_operations"} <= tables
        # Migration is metadata-only: historical terminal vacancies are not
        # silently re-enqueued or re-evaluated.
        assert db.execute("SELECT count(*) FROM pipeline_items").fetchone()[0] == 0
        assert db.execute("SELECT state FROM vacancies WHERE id=992").fetchone()[0] == "SUBMITTED"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO pipeline_model_operations "
                "(session_id, site_id, vacancy_key, stage, role, input_hash, versions_hash, "
                "generation, status, created_at, updated_at) VALUES "
                "(991, 'hh', '', 'evaluation', 'resume_analyst', ?, ?, 0, 'invalid', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("a" * 64, "b" * 64),
            )

    command.downgrade(cfg, "0041")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "pipeline_items" not in tables
        assert db.execute("SELECT state FROM vacancies WHERE id=992").fetchone()[0] == "SUBMITTED"


def test_pipeline_migration_refuses_partial_coordination_schema(tmp_path):
    path = tmp_path / "pipeline-partial.db"
    cfg = config(path)
    command.upgrade(cfg, "0041")
    with sqlite3.connect(path) as db:
        # A shared-process metadata bootstrap may already have materialized
        # the full future shape. Force exactly one surviving 0042 table so
        # the migration's partial-schema safety path is deterministic.
        db.execute("DROP TABLE IF EXISTS pipeline_model_operations")
        db.execute("DROP TABLE IF EXISTS pipeline_checkpoints")
        db.execute("CREATE TABLE IF NOT EXISTS pipeline_items (id INTEGER PRIMARY KEY)")
        db.commit()
    with pytest.raises(RuntimeError, match="Partial 0042 pipeline schema"):
        command.upgrade(cfg, "0042")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0041"
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='pipeline_items'"
        ).fetchone()[0] == 1
