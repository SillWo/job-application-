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


def test_cover_letter_settings_migrate_legacy_rows_and_round_trip(tmp_path):
    path = tmp_path / "cover-letter-settings.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0024")
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into candidate_profiles "
            "(full_name, job_search_locations, contacts, education, languages, created_at, updated_at) "
            "values ('Legacy', '[]', '{}', '[]', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        db.execute("insert into sessions (profile_id, adapter_id, status, counters) values (1, 'hh', 'CREATED', '{}')")
        db.commit()

    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        gender, auto, template = db.execute(
            "select gender, cover_letter_auto, cover_letter_template from candidate_profiles cross join sessions"
        ).fetchone()
        assert gender is None
        assert auto == 1
        assert template == ""
        db.execute("update candidate_profiles set gender = 'female'")
        db.execute("update sessions set cover_letter_auto = 0, cover_letter_template = 'Я [ФИО]'")
        db.commit()
        assert db.execute("select gender from candidate_profiles").fetchone()[0] == "female"
        assert db.execute("select cover_letter_auto, cover_letter_template from sessions").fetchone() == (0, "Я [ФИО]")
        db.execute("update sessions set cover_letter_auto = 1, cover_letter_template = ''")
        db.commit()
        assert db.execute("select cover_letter_auto, cover_letter_template from sessions").fetchone() == (1, "")

    command.downgrade(config, "0024")
    with sqlite3.connect(path) as db:
        assert "gender" not in {row[1] for row in db.execute("pragma table_info(candidate_profiles)")}
        assert "cover_letter_auto" not in {row[1] for row in db.execute("pragma table_info(sessions)")}
