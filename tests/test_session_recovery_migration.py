import json
import sqlite3

from alembic import command
from alembic.config import Config


def test_recovery_migration_preserves_active_work_and_initializes_checkpoint(tmp_path):
    path = tmp_path / "recovery.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0021")
    with sqlite3.connect(path) as db:
        assert "recovery" not in {row[1] for row in db.execute("pragma table_info(sessions)")}
        db.execute("insert into sessions (profile_id, adapter_id, status, counters) values (1, 'hh', 'RUNNING', '{\"submitted\": 37}')")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        status, counters, recovery = db.execute("select status, counters, recovery from sessions").fetchone()
        assert status == "RUNNING"
        assert json.loads(counters) == {"submitted": 37}
        assert json.loads(recovery) == {}
    command.downgrade(config, "0021")
    with sqlite3.connect(path) as db:
        assert db.execute("select status from sessions").fetchone()[0] == "RUNNING"
