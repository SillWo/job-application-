import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import vacancies as vacancy_api
from backend.persistence.database import Base
from backend.persistence.models import Evaluation, Vacancy


@pytest.fixture()
def sql_vacancy_api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(vacancy_api.router)

    def dependency():
        with factory() as db:
            yield db

    app.dependency_overrides[vacancy_api.get_db] = dependency
    with factory() as db:
        rows = []
        for number, state in enumerate(("SUBMITTED", "CANCELLED", "UNCONFIRMED"), 1):
            item = Vacancy(
                source="hh", site="HH.ru", external_id=f"e-{number}", url=f"https://example.test/{number}",
                title=f"Senior Engineer {number}", company="Example", state=state,
                status_changed_at=datetime(2026, 1, number, tzinfo=timezone.utc), data={"big": "x" * 100_000},
            )
            db.add(item); rows.append(item)
        db.flush()
        db.add(Evaluation(vacancy_id=rows[0].id, data={"score": 80, "score_breakdown": {"tasks": 30, "title": 8}}))
        db.commit()
        ids = [row.id for row in rows]
    with TestClient(app) as client:
        yield client, factory, ids
    app.dependency_overrides.clear(); Base.metadata.drop_all(engine); engine.dispose()


def test_list_projection_does_not_select_json_columns():
    sql = str(vacancy_api._select(False).compile(compile_kwargs={"literal_binds": True})).lower()
    assert "vacancies.data" not in sql
    assert "evaluations.data" not in sql
    assert "limit" not in sql


def test_sql_list_search_status_groups_and_detail(sql_vacancy_api):
    client, _, ids = sql_vacancy_api
    response = client.get("/api/vacancies", params={"search": "senior engineer", "limit": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert len(response.json()["items"]) == 1
    assert client.get("/api/vacancies", params={"status_group": "CANCELLED"}).json()["total"] == 1
    assert client.get("/api/vacancies", params={"status_group": "ERROR"}).json()["items"][0]["state"] == "ERROR"
    detail = client.get(f"/api/vacancies/{ids[0]}")
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"
    assert detail.json()["data"]["big"]


def test_projection_indexes_keep_desc_value_and_asc_id_order(sql_vacancy_api):
    _, factory, _ = sql_vacancy_api
    engine = factory.kw["bind"]
    with engine.connect() as connection:
        definitions = dict(connection.execute(text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name LIKE '%projection_%'"
        )).all())
    assert "status_changed_at DESC, id ASC" in definitions["ix_vacancies_projection_date"]
    assert "status_changed_at ASC, id ASC" in definitions["ix_vacancies_projection_date_asc"]
    assert "title_sort DESC, id ASC" in definitions["ix_vacancies_projection_title"]
    assert "total_score DESC, vacancy_id ASC" in definitions["ix_evaluations_projection_total"]
    assert "total_score ASC, vacancy_id ASC" in definitions["ix_evaluations_projection_total_asc"]
    # The names and columns exposed to SQLAlchemy must remain present as well;
    # the direction is deliberately asserted from SQLite's DDL above because
    # Inspector.get_indexes() omits ASC/DESC details.
    names = {item["name"] for item in inspect(engine).get_indexes("vacancies")}
    assert {"ix_vacancies_projection_date", "ix_vacancies_projection_title"} <= names


@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
def test_score_sort_is_null_last_and_id_stable(sql_vacancy_api, sort_dir):
    client, factory, ids = sql_vacancy_api
    with factory() as db:
        rows = db.query(Vacancy).filter(Vacancy.id.in_(ids)).all()
        for row in rows:
            row.status_changed_at = datetime(2026, 1, 10, tzinfo=timezone.utc)
        db.commit()
    response = client.get("/api/vacancies", params={"sort": "total_score", "sort_dir": sort_dir})
    assert response.status_code == 200
    ordered_ids = [item["id"] for item in response.json()["items"]]
    assert ordered_ids == [ids[0], ids[1], ids[2]]


def test_hooks_clear_removed_scores_and_alias_legacy_values(sql_vacancy_api):
    _, factory, ids = sql_vacancy_api
    with factory() as db:
        evaluation = db.query(Evaluation).filter_by(vacancy_id=ids[0]).one()
        assert evaluation.role_match == 8
        evaluation.data = {"score": 10, "score_breakdown": [{"key": "tasks", "points": 2}]}
        db.commit()
        assert evaluation.tasks == 2
        assert evaluation.role_match is None
        assert evaluation.experience_depth is None


@pytest.mark.parametrize("export_format", ["csv", "xlsx", "xml"])
def test_exports_use_same_sql_filter_and_order_and_do_not_capture_route(sql_vacancy_api, export_format):
    client, _, ids = sql_vacancy_api
    response = client.get(
        "/api/vacancies/export",
        params={"format": export_format, "site": "HH.ru", "total_score_min": 80, "sort": "id"},
    )
    assert response.status_code == 200
    assert str(ids[0]).encode() in response.content
    assert client.get(f"/api/vacancies/{ids[0]}").status_code == 200
    if export_format == "csv":
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert len(rows) == 2 and rows[1][0] == str(ids[0])
    elif export_format == "xlsx":
        workbook = load_workbook(io.BytesIO(response.content), read_only=True, data_only=True)
        assert list(workbook.active.values)[1][0] == ids[0]
    else:
        root = ET.fromstring(response.content)
        assert root.find("./vacancy/field").text == str(ids[0])
