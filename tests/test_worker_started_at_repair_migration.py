from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def _config(path):
    result = Config("alembic.ini")
    result.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return result


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def test_repair_migration_adds_missing_worker_started_at_preserving_execution(tmp_path):
    path = tmp_path / "historical-0042.db"
    cfg = _config(path)
    command.upgrade(cfg, "0042")

    with sqlite3.connect(path) as db:
        # This reproduces a database whose earlier 0040 had already been
        # stamped before worker_started_at was added to that migration.
        db.execute("ALTER TABLE session_execution DROP COLUMN worker_started_at")
        db.execute(
            "INSERT INTO sessions (id, adapter_id, status, counters, desired_job_description, recovery) "
            "VALUES (993, 'hh', 'PREPARING', '{}', '', '{}')"
        )
        db.execute(
            "INSERT INTO session_execution "
            "(id, session_id, stage, stage_started_at, last_progress_at, start_requested, generation, worker_pid) "
            "VALUES (994, 993, 'RUNNING', '2026-09-20 12:00:00', '2026-09-20 12:01:00', 1, 7, 12345)"
        )
        db.commit()
        before = db.execute(
            "SELECT id, session_id, stage, stage_started_at, last_progress_at, start_requested, generation, worker_pid "
            "FROM session_execution WHERE id=994"
        ).fetchone()
        assert "worker_started_at" not in _columns(db, "session_execution")
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0042"

    command.upgrade(cfg, "head")

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            ScriptDirectory.from_config(cfg).get_current_head()
        )
        assert "worker_started_at" in _columns(db, "session_execution")
        after = db.execute(
            "SELECT id, session_id, stage, stage_started_at, last_progress_at, start_requested, generation, worker_pid "
            "FROM session_execution WHERE id=994"
        ).fetchone()
        assert after == before
        assert db.execute(
            "SELECT worker_started_at FROM session_execution WHERE id=994"
        ).fetchone()[0] is None


def test_fresh_database_has_worker_started_at_after_head_upgrade(tmp_path):
    path = tmp_path / "fresh.db"
    cfg = _config(path)
    command.upgrade(cfg, "head")

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            ScriptDirectory.from_config(cfg).get_current_head()
        )
        assert "worker_started_at" in _columns(db, "session_execution")
