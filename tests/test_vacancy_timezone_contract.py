from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import vacancies as vacancy_api
from backend.persistence.database import Base
from backend.persistence.models import Evaluation, Vacancy


@pytest.fixture
def timezone_api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(vacancy_api.router)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[vacancy_api.get_db] = database
    timestamps = [
        "2026-01-01T16:59:59+00:00",
        "2026-01-01T17:00:00+00:00",
        "2026-01-02T04:59:59+00:00",
        "2026-01-02T05:00:00+00:00",
        "2026-01-02T16:59:59+00:00",
        "2026-01-02T17:00:00+00:00",
        "2026-01-03T04:59:59+00:00",
        "2026-01-03T05:00:00+00:00",
    ]
    with factory() as db:
        rows = []
        for index, timestamp in enumerate(timestamps, 1):
            item = Vacancy(
                source="hh",
                site="" if index == 1 else "HH.ru",
                external_id=f"tz-{index}",
                url=f"https://example.test/{index}",
                title=f"Vacancy {index}",
                state="SUBMITTED",
                status_changed_at=datetime.fromisoformat(timestamp),
                data={},
            )
            db.add(item)
            rows.append(item)
        db.flush()
        for index, row in enumerate(rows, 1):
            db.add(Evaluation(vacancy_id=row.id, data={"score": index * 10}))
        db.commit()
        ids = [row.id for row in rows]
    with TestClient(app) as client:
        yield client, ids
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _ids_from_export(response, format):
    if format == "csv":
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        return [int(row[0]) for row in rows[1:]]
    if format == "xlsx":
        workbook = load_workbook(io.BytesIO(response.content), read_only=True, data_only=True)
        return [row[0] for row in list(workbook.active.values)[1:]]
    root = ET.fromstring(response.content)
    return [int(node.find("./field").text) for node in root.findall("./vacancy")]


def test_positive_offset_local_day_filters_count_pages_score_sort_and_exports(timezone_api):
    client, ids = timezone_api
    params = {
        "status_time_from": "2026-01-02T00:00:00+07:00",
        "status_time_before": "2026-01-03T00:00:00+07:00",
        "sort": "total_score",
        "sort_dir": "desc",
        "limit": 1,
    }
    first = client.get("/api/vacancies", params=params)
    assert first.status_code == 200
    assert first.json()["total"] == 4
    assert first.json()["has_more"] is True
    assert [row["id"] for row in first.json()["items"]] == [ids[4]]

    second = client.get("/api/vacancies", params={**params, "offset": 1})
    assert second.json()["total"] == 4
    assert second.json()["has_more"] is True
    assert [row["id"] for row in second.json()["items"]] == [ids[3]]

    final_page = client.get("/api/vacancies", params={**params, "offset": 3})
    assert final_page.json()["total"] == 4
    assert final_page.json()["has_more"] is False
    assert [row["id"] for row in final_page.json()["items"]] == [ids[1]]

    export_params = {key: value for key, value in params.items() if key != "limit"}
    expected = [ids[4], ids[3], ids[2], ids[1]]
    for format in ("csv", "xlsx", "xml"):
        response = client.get("/api/vacancies/export", params={**export_params, "format": format})
        assert response.status_code == 200
        assert _ids_from_export(response, format) == expected


def test_negative_offset_local_day_uses_aware_utc_normalization(timezone_api):
    client, ids = timezone_api
    response = client.get(
        "/api/vacancies",
        params={
            "status_time_from": "2026-01-02T00:00:00-05:00",
            "status_time_before": "2026-01-03T00:00:00-05:00",
            "sort": "id",
            "sort_dir": "asc",
        },
    )
    assert response.status_code == 200
    assert response.json()["total"] == 4
    assert [row["id"] for row in response.json()["items"]] == [ids[3], ids[4], ids[5], ids[6]]


@pytest.mark.parametrize(
    "params",
    [
        {"status_time_from": "2026-01-02T00:00:00"},
        {"status_time_before": "2026-01-03T00:00:00"},
        {
            "status_time_from": "2026-01-03T00:00:00+00:00",
            "status_time_before": "2026-01-02T00:00:00+00:00",
        },
        {
            "status_time_from": "2026-01-02T00:00:00+00:00",
            "status_date_from": "2026-01-02",
        },
    ],
)
def test_invalid_or_ambiguous_time_ranges_return_422(timezone_api, params):
    client, _ids = timezone_api
    assert client.get("/api/vacancies", params=params).status_code == 422
    assert client.get("/api/vacancies/export", params=params).status_code == 422


def test_legacy_date_filters_keep_utc_calendar_semantics(timezone_api):
    client, ids = timezone_api
    response = client.get(
        "/api/vacancies",
        params={"status_date_from": "2026-01-02", "status_date_to": "2026-01-02", "sort": "id", "sort_dir": "asc"},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 4
    assert [row["id"] for row in response.json()["items"]] == ids[2:6]


def test_exports_treat_naive_site_less_timestamp_as_utc():
    value = datetime(2026, 1, 2, 0, 30)
    actual = vacancy_api._export_status_time(value, None)
    expected = value.replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")
    assert actual == expected
    assert vacancy_api._status_time(value) == "2026-01-02T00:30:00+00:00"
