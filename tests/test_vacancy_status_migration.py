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
    columns = {row[1] for row in connection.execute("pragma table_info(vacancies)")}
    assert "status_changed_at" not in columns
    assert "site" not in columns
    assert not {
        "search_text",
        "title_sort",
        "site_sort",
        "error_code",
        "error_message",
    } & columns
    indexes = {row[1] for row in connection.execute("pragma index_list(vacancies)")}
    assert not {name for name in indexes if name.startswith("ix_vacancies_projection_")}
    evaluation_columns = {
        row[1] for row in connection.execute("pragma table_info(evaluations)")
    }
    assert not {
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
    } & evaluation_columns
    evaluation_indexes = {
        row[1] for row in connection.execute("pragma index_list(evaluations)")
    }
    assert not {
        name
        for name in evaluation_indexes
        if name.startswith("ix_evaluations_projection_")
    }
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
    assert not {name for name in indexes if name.startswith("ix_vacancies_projection_")}
    connection.close()

    command.downgrade(config, "0020")
    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("pragma table_info(vacancies)")}
    assert "status_changed_at" not in columns
    assert "site" not in columns
    connection.close()
