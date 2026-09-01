import sqlite3

from alembic import command
from alembic.config import Config


def _upgrade(path, revision):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, revision)


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def test_history_cleanup_preserves_good_history(tmp_path):
    path = tmp_path / "history.db"
    _upgrade(path, "0014")
    db = sqlite3.connect(path)
    db.execute("insert into candidate_profiles (full_name, job_search_locations, contacts, education, languages, created_at, updated_at) values ('test', '[]', '{}', '[]', '[]', '2026-01-01', '2026-01-01')")
    profile = db.execute("select id from candidate_profiles").fetchone()[0]
    db.executemany(
        "insert into sessions (profile_id, adapter_id, status, counters) values (?, ?, 'COMPLETED', '{}')",
        [(profile, "hh"), (profile, "hirehi")],
    )
    hh, hirehi = [row[0] for row in db.execute("select id from sessions order by id")]
    db.executemany(
        "insert into vacancies (session_id, source, site, external_id, url, title, state, "
        "status_changed_at, data, updated_at) values (?, ?, '', ?, ?, ?, ?, "
        "'2026-09-01 00:00:00', '{}', '2026-01-01')",
        [
            (hh, "hh", "good", "https://example.test/good", "Good", "SUBMITTED"),
            (hh, "hh", "bad", "https://example.test/bad", "Bad", "ERROR"),
            (hirehi, "hirehi", "good-hi", "https://example.test/hi", "HireHi", "REPORTED"),
            (hirehi, "hirehi", "bad-hi", "https://example.test/bad-hi", "Bad HireHi", "UNKNOWN"),
        ],
    )
    vacancies = dict(db.execute("select external_id, id from vacancies"))
    db.execute("insert into evaluations (vacancy_id, data) values (?, '{}')", (vacancies["good"],))
    db.execute("insert into evaluations (vacancy_id, data) values (?, '{}')", (vacancies["bad"],))
    db.execute("insert into browser_events (session_id, event_type, message, data, created_at) values (?, 'x', 'x', '{}', '2026-01-01')", (hh,))
    db.execute("insert into browser_events (session_id, event_type, message, data, created_at) values (?, 'x', 'x', '{}', '2026-01-01')", (hirehi,))
    db.commit()
    db.close()

    _upgrade(path, "head")
    db = sqlite3.connect(path)
    assert db.execute("select adapter_id from sessions").fetchall() == [("hirehi",)]
    assert db.execute("select source, state, session_id from vacancies order by source").fetchall() == [("hh", "SUBMITTED", None), ("hirehi", "REPORTED", hirehi)]
    assert db.execute("select count(*) from evaluations").fetchone()[0] == 1
    assert db.execute("select count(*) from browser_events").fetchone()[0] == 1
    tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    assert not {"search_policies", "site_accounts", "employer_contacts"} & tables
    assert not {"policy_id", "resume_url", "resume_path"} & _columns(db, "sessions")

    assert "application_limit" in _columns(db, "sessions")
    assert not {"filename", "data", "resume_path"} & _columns(db, "candidate_profiles")
    db.close()


def test_fresh_upgrade_head_isolated(tmp_path):
    path = tmp_path / "fresh.db"
    _upgrade(path, "head")
    db = sqlite3.connect(path)
    assert "vacancies" in {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    assert "session_id" in _columns(db, "vacancies")
    assert {"desired_job_description", "preference_policy"} <= _columns(db, "sessions")
    assert "viewed_limit" not in _columns(db, "sessions")
    assert "application_limit" in _columns(db, "sessions")
    db.close()


def test_database_url_environment_override_wins_without_touching_default(tmp_path, monkeypatch):
    target = tmp_path / "env.db"
    decoy = tmp_path / "decoy.db"
    default = sqlite3.connect("data/orchestrator.db")
    default_revision = default.execute("select version_num from alembic_version").fetchone()
    default.close()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{decoy.as_posix()}")
    monkeypatch.setenv("JAO_DATABASE_URL", f"sqlite:///{target.as_posix()}")
    command.upgrade(config, "head")

    target_db = sqlite3.connect(target)
    assert target_db.execute("select version_num from alembic_version").fetchone()[0] == "0021"
    assert "vacancies" in {row[0] for row in target_db.execute("select name from sqlite_master where type='table'")}
    target_db.close()
    assert not decoy.exists()

    default = sqlite3.connect("data/orchestrator.db")
    assert default.execute("select version_num from alembic_version").fetchone() == default_revision
    default.close()
