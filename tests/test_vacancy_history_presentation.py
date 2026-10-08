from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import vacancies as api
from backend.persistence.database import Base
from backend.persistence.models import Application, Vacancy
from backend.services.vacancy_history_presentation import resolve_external_history


@pytest.fixture
def history_api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(api.router)

    def db_override():
        with factory() as db:
            yield db

    app.dependency_overrides[api.get_db] = db_override
    with factory() as db:
        confirmed = Vacancy(source="hh", external_id="confirmed", url="https://example.test/1", title="Confirmed", state="SUBMITTED", data={})
        uncertain = Vacancy(source="hh", external_id="uncertain", url="https://example.test/2", title="Uncertain", state="SUBMISSION_UNCONFIRMED", error_code="SUBMISSION_UNCONFIRMED", data={"error_code": "SUBMISSION_UNCONFIRMED"})
        partial = Vacancy(source="hh", external_id="partial", url="https://example.test/3", title="Partial", state="PARTIAL", data={"submission_progress": {"cv_confirmed": True}})
        genuine = Vacancy(source="hh", external_id="genuine", url="https://example.test/4", title="Observed already applied", state="ALREADY_APPLIED", data={})
        db.add_all([confirmed, uncertain, partial, genuine])
        db.flush()
        aliases = []
        for source in (confirmed, uncertain, partial, genuine):
            alias = Vacancy(
                source="hh", external_id=source.external_id, url=source.url, title=source.title,
                state="ALREADY_APPLIED",
                data={"cross_session_suppressed": True, "historical_vacancy_id": source.id},
            )
            db.add(alias)
            aliases.append(alias)
        db.flush()
        chain = Vacancy(
            source="hh", external_id="uncertain", url=uncertain.url, title="Uncertain chain",
            state="ALREADY_APPLIED",
            data={"cross_session_suppressed": True, "historical_vacancy_id": aliases[1].id},
        )
        malformed = Vacancy(
            source="hh", external_id="missing", url="https://example.test/5", title="Missing source",
            state="ALREADY_APPLIED",
            data={"cross_session_suppressed": True, "historical_vacancy_id": 999999},
        )
        db.add_all([chain, malformed])
        db.commit()
        ids = {"confirmed": aliases[0].id, "uncertain": aliases[1].id, "uncertain_source": uncertain.id, "partial": aliases[2].id,
               "genuine": genuine.id, "chain": chain.id, "malformed": malformed.id}
    try:
        yield TestClient(app), factory, ids
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_resolver_distinguishes_source_outcomes_and_fails_closed(history_api):
    _, factory, ids = history_api
    with factory() as db:
        resolutions = {key: resolve_external_history(db, db.get(Vacancy, value)) for key, value in ids.items()}
    assert resolutions["confirmed"].outcome == "confirmed"
    assert resolutions["genuine"].outcome == "already_applied"
    assert resolutions["partial"].outcome == "partial"
    assert resolutions["uncertain"].outcome == "unconfirmed"
    assert resolutions["chain"].origin.external_id == "uncertain"
    assert resolutions["malformed"].invalid_history is True
    assert resolutions["malformed"].outcome == "unconfirmed"
    with factory() as db:
        source = db.get(Vacancy, ids["uncertain_source"])
        wrong_identity = Vacancy(
            source="hh", external_id="some-other-vacancy", url=source.url, title="Wrong identity",
            state="ALREADY_APPLIED", data={"cross_session_suppressed": True, "historical_vacancy_id": source.id},
        )
        self_cycle = Vacancy(
            source="hh", external_id="cycle", url="https://example.test/c", title="Cycle",
            state="ALREADY_APPLIED", data={"cross_session_suppressed": True},
        )
        db.add_all([wrong_identity, self_cycle])
        db.flush()
        self_cycle.data = {"cross_session_suppressed": True, "historical_vacancy_id": self_cycle.id}
        db.flush()
        assert resolve_external_history(db, wrong_identity).invalid_history is True
        assert resolve_external_history(db, self_cycle).invalid_history is True
        assert resolve_external_history(db, db.get(Vacancy, ids["chain"]), max_depth=1).invalid_history is True


def test_list_detail_filters_count_and_export_share_effective_history_state(history_api):
    client, _, ids = history_api
    errors = client.get("/api/vacancies", params={"status_group": "ERROR"}).json()
    uncertain_ids = {ids["uncertain"], ids["uncertain_source"], ids["chain"], ids["malformed"]}
    assert errors["total"] == len(uncertain_ids)
    by_id = {item["id"]: item for item in errors["items"]}
    assert set(by_id) == uncertain_ids
    for item in by_id.values():
        assert item["state"] == "SUBMISSION_UNCONFIRMED"
        assert item["status_group"] == "ERROR"
        assert item["error_code"] == "SUBMISSION_UNCONFIRMED"
        expected_analysis = "not_evaluated" if item["id"] == ids["uncertain_source"] else "not_evaluated_history"
        assert item["analysis_status"] == expected_analysis
        assert item["evaluation"] is None
    detail = client.get(f"/api/vacancies/{ids['chain']}").json()
    assert detail["history_context"]["source_vacancy_id"] is not None
    assert client.get("/api/vacancies", params={"state": "SUBMISSION_UNCONFIRMED"}).json()["total"] == len(uncertain_ids)
    csv_response = client.get("/api/vacancies/export", params={"status_group": "ERROR", "format": "csv"})
    assert csv_response.status_code == 200
    assert all(str(item_id) in csv_response.text for item_id in uncertain_ids)


def test_processing_unscored_is_pending_and_real_application_confirms(history_api):
    client, factory, _ = history_api
    with factory() as db:
        pending = Vacancy(source="hh", external_id="pending", url="https://example.test/p", title="Pending", state="EVALUATING", data={})
        applied = Vacancy(source="hh", external_id="app", url="https://example.test/a", title="App", state="EXTRACTED", data={})
        db.add_all([pending, applied])
        db.flush()
        db.add(Application(vacancy_id=applied.id, status="submitted", submitted_at=datetime.now(timezone.utc)))
        db.commit()
        pending_id, applied_id = pending.id, applied.id
    pending_item = client.get(f"/api/vacancies/{pending_id}").json()
    assert pending_item["analysis_status"] == "pending"
    with factory() as db:
        applied_row = db.get(Vacancy, applied_id)
        assert resolve_external_history(db, applied_row).outcome == "confirmed"


def test_intermediate_alias_evidence_precedes_older_uncertain_source(history_api):
    client, factory, _ = history_api
    with factory() as db:
        source = Vacancy(source="hh", external_id="ancestor-app", url="https://example.test/x", title="Source", state="ERROR", data={"error_code": "SUBMISSION_UNCONFIRMED"})
        db.add(source)
        db.flush()
        intermediate = Vacancy(
            source="hh", external_id=source.external_id, url=source.url, title="Intermediate",
            state="ALREADY_APPLIED", data={"cross_session_suppressed": True, "historical_vacancy_id": source.id},
        )
        db.add(intermediate)
        db.flush()
        db.add(Application(vacancy_id=intermediate.id, status="submitted"))
        latest = Vacancy(
            source="hh", external_id=source.external_id, url=source.url, title="Latest",
            state="ALREADY_APPLIED", data={"cross_session_suppressed": True, "historical_vacancy_id": intermediate.id},
        )
        db.add(latest)
        db.commit()
        latest_id, intermediate_id = latest.id, intermediate.id
        assert resolve_external_history(db, latest).origin.id == intermediate_id
        assert resolve_external_history(db, latest).outcome == "confirmed"
    api_item = client.get(f"/api/vacancies/{latest_id}").json()
    assert api_item["status_group"] == "SUCCESS"
    assert api_item["history_context"]["source_vacancy_id"] == intermediate_id


def test_large_history_list_uses_bounded_query_count(history_api):
    client, factory, _ = history_api
    with factory() as db:
        source = Vacancy(source="hh", external_id="bulk-history", url="https://example.test/bulk", title="Bulk", state="SUBMISSION_UNCONFIRMED", data={"error_code": "SUBMISSION_UNCONFIRMED"})
        db.add(source)
        db.flush()
        db.add_all([
            Vacancy(
                source="hh", external_id=source.external_id, url=source.url, title=f"Bulk {index}",
                state="ALREADY_APPLIED",
                data={"cross_session_suppressed": True, "historical_vacancy_id": source.id},
            )
            for index in range(40)
        ])
        db.commit()
    statements = []
    engine = factory.kw["bind"]
    def count_statement(*_args):
        statements.append(1)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        response = client.get("/api/vacancies", params={"status_group": "ERROR", "limit": 100})
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert response.status_code == 200
    assert response.json()["total"] >= 41
    assert len(statements) <= 4
