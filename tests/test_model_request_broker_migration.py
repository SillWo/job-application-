from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config


def config(path):
    result = Config("alembic.ini")
    result.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return result


def test_broker_migration_empty_upgrade_and_downgrade(tmp_path):
    path = tmp_path / "empty.db"
    command.upgrade(config(path), "0041")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        assert {"model_requests", "model_response_cache", "model_generation_health"} <= tables
        indexes = {
            row[0]
            for row in db.execute(
                "select name from sqlite_master where type='index' and tbl_name='model_requests'"
            )
        }
        assert {
            "ix_model_requests_schedule",
            "ix_model_requests_site_fifo",
            "ix_model_requests_session_status",
            "ix_model_requests_stale",
        } <= indexes
        foreign_keys = list(db.execute("pragma foreign_key_list(model_requests)"))
        assert any(row[2] == "sessions" and row[3] == "session_id" for row in foreign_keys)
        assert any(
            row[2] == "model_requests" and row[3] == "cache_source_request_id"
            for row in foreign_keys
        )
        health_foreign_keys = list(
            db.execute("pragma foreign_key_list(model_generation_health)")
        )
        assert {
            row[3] for row in health_foreign_keys if row[2] == "model_requests"
        } == {"last_success_request_id", "last_failure_request_id"}
        table_sql = db.execute(
            "select sql from sqlite_master where type='table' and name='model_requests'"
        ).fetchone()[0]
        assert "attempt <= max_attempts" in table_sql
        assert "deadline_at > created_at" in table_sql
        assert "canonical_output IS NOT NULL" in table_sql
    command.downgrade(config(path), "0040")
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        assert "model_requests" not in tables
        assert "model_response_cache" not in tables
        assert "model_generation_health" not in tables


def test_broker_migration_preserves_historical_sessions_without_reprocessing(tmp_path):
    path = tmp_path / "historical.db"
    command.upgrade(config(path), "0040")
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into sessions (adapter_id, status, counters) values "
            "('hh', 'COMPLETED', '{\"submitted\": 9}')"
        )
        session_id = db.execute("select id from sessions").fetchone()[0]
        before = db.execute(
            "select id, adapter_id, status, counters from sessions where id = ?", (session_id,)
        ).fetchone()
        db.commit()

    command.upgrade(config(path), "0041")
    command.upgrade(config(path), "head")
    with sqlite3.connect(path) as db:
        after = db.execute(
            "select id, adapter_id, status, counters from sessions where id = ?", (session_id,)
        ).fetchone()
        assert after == before
        assert db.execute("select count(*) from model_requests").fetchone() == (0,)
        assert db.execute("select count(*) from model_response_cache").fetchone() == (0,)

    command.downgrade(config(path), "0040")
    with sqlite3.connect(path) as db:
        assert db.execute(
            "select id, adapter_id, status, counters from sessions where id = ?", (session_id,)
        ).fetchone() == before
