from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _migrate(path: Path, revision: str) -> None:
    command.upgrade(_config(path), revision)


def _downgrade(path: Path, revision: str) -> None:
    command.downgrade(_config(path), revision)


def _columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("pragma table_info(sessions)")}


def test_hirehi_pro_migration_preserves_rows_and_supports_downgrade(tmp_path: Path):
    path = tmp_path / "hirehi-pro.db"
    _migrate(path, "0037")
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into sessions (adapter_id, status, counters) values ('hirehi', 'CREATED', '{}')"
        )
        db.commit()

    _migrate(path, "0038")
    with sqlite3.connect(path) as db:
        assert "hirehi_pro_enabled" in _columns(db)
        assert db.execute("select hirehi_pro_enabled from sessions").fetchall() == [(0,)]

        # Existing clients that do not know the new column remain compatible.
        db.execute(
            "insert into sessions (adapter_id, status, counters) values ('hh', 'CREATED', '{}')"
        )
        db.commit()
        assert db.execute("select hirehi_pro_enabled from sessions order by id").fetchall() == [(0,), (0,)]

    # Replaying the target upgrade/head is a no-op and does not duplicate data.
    _migrate(path, "head")
    _migrate(path, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from sessions").fetchone() == (2,)

    _downgrade(path, "0037")
    with sqlite3.connect(path) as db:
        assert "hirehi_pro_enabled" not in _columns(db)
        assert db.execute("select count(*) from sessions").fetchone() == (2,)
