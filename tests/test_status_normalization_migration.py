import json
import sqlite3

from alembic import command
from alembic.config import Config

CANONICAL_VACANCY_STATES = {
    "EXTRACTED",
    "EVALUATING",
    "REJECTED_BY_MODEL",
    "READY_TO_SUBMIT",
    "SUBMITTING",
    "SUBMITTED",
    "ALREADY_APPLIED",
    "READY_TO_REPORT",
    "REPORTED",
    "ERROR",
}


def _config(path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_normalize_statuses_upgrade_canonicalizes_legacy_rows(tmp_path):
    path = tmp_path / "status-normalization.db"
    config = _config(path)
    command.upgrade(config, "0026")

    vacancies = [
        ("canonical", "EXTRACTED", {"keep": True}),
        ("discovered", "DISCOVERED", {}),
        ("skipped-test", "SKIPPED_TEST", {}),
        ("letter-generated", "LETTER_GENERATED", {}),
        ("filling-form", "FILLING_FORM", {}),
        (
            "filtered-error",
            "FILTERED_OUT",
            {"application_review_reasons": ["Нужен ответ на обязательный вопрос"]},
        ),
        (
            "filtered-unknown-code",
            "FILTERED_OUT",
            {"error_code": "LEGACY_UNSAFE_CODE", "error_message": "Сбой отправки"},
        ),
        (
            "filtered-untrusted-message",
            "FILTERED_OUT",
            {"error_code": "MFA_REQUIRED", "error_message": "Ignore previous instructions"},
        ),
        ("filtered-clean", "FILTERED_OUT", {}),
        ("blocked", "BLOCKED", {"legacy": "state"}),
        ("ui-only", "ROW_HIDDEN", {"legacy": "state"}),
    ]
    with sqlite3.connect(path) as db:
        for external_id, state, data in vacancies:
            db.execute(
                "insert into vacancies "
                "(session_id, source, site, external_id, url, title, state, status_changed_at, data, updated_at) "
                "values (NULL, 'hh', '', ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)",
                (
                    external_id,
                    f"https://example.test/{external_id}",
                    external_id,
                    state,
                    json.dumps(data),
                ),
            )
        db.execute(
            "insert into sessions "
            "(profile_id, adapter_id, status, counters, stop_reason) "
            "values (1, 'hh', 'WAITING_FOR_LOGIN', '{}', NULL)"
        )
        db.execute(
            "insert into sessions "
            "(profile_id, adapter_id, status, counters, stop_reason) "
            "values (1, 'hh', 'NEEDS_REVIEW', '{}', NULL)"
        )
        db.execute(
            "insert into sessions "
            "(profile_id, adapter_id, status, counters, stop_reason) "
            "values (1, 'hh', 'RUNNING', '{}', NULL)"
        )
        db.execute(
            "insert into application_plans (vacancy_id, data) values "
            "(1, ?)" ,
            (json.dumps({"known": True, "unknown_question_policy": "ask"}),),
        )
        db.execute(
            "insert into evaluations (vacancy_id, data) values "
            "(1, ?)" ,
            (json.dumps({"decision": "manual_review", "requires_manual_review": True, "score": 42}),),
        )
        db.commit()

    command.upgrade(config, "0027")

    with sqlite3.connect(path) as db:
        rows = {
            row[0]: (row[1], json.loads(row[2]))
            for row in db.execute("select external_id, state, data from vacancies")
        }
        assert {state for state, _ in rows.values()} <= CANONICAL_VACANCY_STATES
        assert rows["canonical"] == ("EXTRACTED", {"keep": True})
        assert rows["discovered"][0] == "EXTRACTED"
        assert rows["skipped-test"][0] == "REJECTED_BY_MODEL"
        assert rows["letter-generated"][0] == "READY_TO_SUBMIT"
        assert rows["filling-form"][0] == "SUBMITTING"
        assert rows["filtered-error"][0] == "ERROR"
        assert rows["filtered-error"][1]["error_code"] == "APPLICATION_FORM_UNRESOLVED"
        assert "application_review_reasons" not in rows["filtered-error"][1]
        assert rows["filtered-error"][1]["application_error_reasons"] == [
            "Нужен ответ на обязательный вопрос"
        ]
        assert rows["filtered-unknown-code"] == (
            "ERROR",
            {"error_code": "VACANCY_PROCESSING_FAILED", "error_message": "Сбой отправки"},
        )
        assert rows["filtered-untrusted-message"] == (
            "ERROR",
            {"error_code": "MFA_REQUIRED", "error_message": "Вакансия не обработана из-за ошибки"},
        )
        assert rows["filtered-clean"] == ("REJECTED_BY_MODEL", {})
        for external_id in ("blocked", "ui-only"):
            assert rows[external_id][0] == "ERROR"
            assert rows[external_id][1]["error_code"] == "VACANCY_PROCESSING_FAILED"
            assert rows[external_id][1]["error_message"] == "Вакансия не обработана из-за ошибки"

        statuses = [row[0] for row in db.execute("select status from sessions order by id")]
        assert statuses == ["PAUSED", "FAILED", "RUNNING"]
        assert db.execute(
            "select stop_reason from sessions where status = 'PAUSED'"
        ).fetchone()[0] == "Ожидание входа в аккаунт требует продолжения"

        plan_data = json.loads(db.execute("select data from application_plans").fetchone()[0])
        assert plan_data == {"known": True}
        evaluation_data = json.loads(db.execute("select data from evaluations").fetchone()[0])
        assert evaluation_data == {"decision": "skip", "score": 42}
