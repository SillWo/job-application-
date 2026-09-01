import sqlite3

from alembic import command
from alembic.config import Config


def _config(path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_legacy_vacancies_receive_exact_status_time_and_blank_site(tmp_path):
    path = tmp_path / "legacy-vacancy.db"
    config = _config(path)
    command.upgrade(config, "0020")
    connection = sqlite3.connect(path)
    # Migration 0001 creates current metadata on a fresh database, so remove
    # the two model-leaked columns to reproduce a genuine pre-0021 database.
    indexes = {row[1] for row in connection.execute("pragma index_list(vacancies)")}
    if "ix_vacancies_status_changed_at" in indexes:
        connection.execute("drop index ix_vacancies_status_changed_at")
    columns = {row[1] for row in connection.execute("pragma table_info(vacancies)")}
    if "status_changed_at" in columns:
        connection.execute("alter table vacancies drop column status_changed_at")
    if "site" in columns:
        connection.execute("alter table vacancies drop column site")
    connection.execute(
        "insert into vacancies "
        "(session_id, source, external_id, url, title, state, data, updated_at) "
        "values (NULL, 'hh', 'legacy', 'https://example.test/legacy', "
        "'Legacy', 'SUBMITTED', '{}', '2026-08-01 10:00:00')"
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "0021")
    connection = sqlite3.connect(path)
    assert connection.execute(
        "select status_changed_at, site from vacancies where external_id='legacy'"
    ).fetchone() == ("2026-09-01 00:00:00", "")
    columns = {row[1]: row for row in connection.execute("pragma table_info(vacancies)")}
    assert columns["status_changed_at"][3] == 1
    assert columns["site"][3] == 1
    indexes = {row[1] for row in connection.execute("pragma index_list(vacancies)")}
    assert "ix_vacancies_status_changed_at" in indexes
    connection.close()

    command.downgrade(config, "0020")
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("pragma table_info(vacancies)")}
    assert "status_changed_at" not in columns
    assert "site" not in columns
    connection.close()
