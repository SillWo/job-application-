import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.persistence.database import Base
from backend.persistence.models import Evaluation, Vacancy

CURRENT_BREAKDOWN = [
    {"key": "tasks", "points": 30},
    {"key": "skills", "points": 18},
    {"key": "experience_depth", "points": 12},
    {"key": "role_match", "points": 8},
    {"key": "industry", "points": 9},
    {"key": "special_requirements", "points": 7},
]
LEGACY_BREAKDOWN = [
    {"key": "tasks", "points": 10},
    {"key": "skills", "points": 5},
    {"key": "required_years", "points": 6},
    {"key": "title", "points": 4},
    {"key": "industry", "points": 2},
    {"key": "languages", "points": 1},
]


def _session(factory):
    with factory() as db:
        yield db


@pytest.fixture()
def vacancy_api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = lambda: (yield from _session(session_factory))

    with session_factory() as db:
        legacy = Vacancy(
            source="hh",
            site="",
            external_id="legacy-needle",
            url="https://example.test/legacy",
            title="Legacy analyst",
            company="Old Company",
            state="REJECTED_BY_MODEL",
            status_changed_at=datetime(2026, 9, 1, 0, 0),
            data={},
        )
        current = Vacancy(
            source="hh",
            site="HH.ru",
            external_id="hh-current",
            url="https://example.test/current",
            title="Product manager",
            company="Current Company",
            state="SUBMITTED",
            status_changed_at=datetime(2026, 9, 2, 12, 30),
            data={},
        )
        hirehi = Vacancy(
            source="hirehi",
            site="HireHi",
            external_id="global-search-target",
            url="https://example.test/hirehi",
            title="Unique global vacancy",
            company="Search Company",
            state="REPORTED",
            status_changed_at=datetime(2026, 9, 3, 9, 15),
            data={},
        )
        missing = Vacancy(
            source="zarplata",
            site="Zarplata.ru",
            external_id="no-evaluation",
            url="https://example.test/missing",
            title="No evaluation",
            company=None,
            state="ERROR",
            status_changed_at=datetime(2026, 9, 4, 18, 45),
            data={},
        )
        db.add_all([legacy, current, hirehi, missing])
        db.flush()
        db.add_all(
            [
                Evaluation(
                    vacancy_id=legacy.id,
                    data={"score": 40, "score_breakdown": LEGACY_BREAKDOWN},
                ),
                Evaluation(
                    vacancy_id=current.id,
                    data={"score": 80, "score_breakdown": CURRENT_BREAKDOWN},
                ),
                Evaluation(
                    vacancy_id=hirehi.id,
                    data={
                        "score": 60,
                        "score_breakdown": [
                            {"key": row["key"], "points": row["points"] - 1}
                            for row in CURRENT_BREAKDOWN
                        ],
                    },
                ),
            ]
        )
        db.commit()
        ids = {
            "legacy": legacy.id,
            "current": current.id,
            "hirehi": hirehi.id,
            "missing": missing.id,
        }

    yield TestClient(app), session_factory, ids
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_public_vacancy_fields_and_full_database_search_before_pagination(vacancy_api):
    client, _, ids = vacancy_api
    page = client.get("/api/vacancies", params={"limit": 1, "search": "global-search"})
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 1
    assert body["has_more"] is False
    assert body["items"][0]["id"] == ids["hirehi"]
    assert body["items"][0]["site"] == "HireHi"
    assert body["items"][0]["source"] == "hirehi"
    assert body["items"][0]["status_changed_at"] == "2026-09-03T09:15:00+00:00"

    by_number = client.get("/api/vacancies", params={"search": str(ids["legacy"])})
    assert ids["legacy"] in {item["id"] for item in by_number.json()["items"]}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"state": "SUBMITTED"}, "current"),
        ({"site": "HH.ru"}, "current"),
        ({"site": "__legacy__"}, "legacy"),
        ({"status_date_from": "2026-09-02", "status_date_to": "2026-09-02"}, "current"),
        ({"total_score_min": 80, "total_score_max": 80}, "current"),
        ({"tasks_min": 30, "tasks_max": 30}, "current"),
        ({"skills_min": 18, "skills_max": 18}, "current"),
        ({"experience_depth_min": 12, "experience_depth_max": 12}, "current"),
        ({"role_match_min": 8, "role_match_max": 8}, "current"),
        ({"industry_min": 9, "industry_max": 9}, "current"),
        ({"special_requirements_min": 7, "special_requirements_max": 7}, "current"),
        ({"experience_depth_min": 6, "experience_depth_max": 6}, "legacy"),
        ({"role_match_min": 4, "role_match_max": 4}, "legacy"),
        ({"special_requirements_min": 1, "special_requirements_max": 1}, "legacy"),
    ],
)
def test_vacancy_filters_cover_status_date_site_total_and_every_score(
    vacancy_api, params, expected
):
    client, _, ids = vacancy_api
    response = client.get("/api/vacancies", params=params)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [ids[expected]]


def test_status_filter_groups_rejected_and_error_states(vacancy_api):
    client, session_factory, _ = vacancy_api
    with session_factory() as db:
        db.add_all(
            [
                Vacancy(source="test", site="", external_id="filtered", url="https://example.test/filtered", title="Filtered", state="FILTERED_OUT", data={}),
                Vacancy(source="test", site="", external_id="failed", url="https://example.test/failed", title="Failed", state="FAILED", data={}),
                Vacancy(source="test", site="", external_id="unknown-result", url="https://example.test/unknown-result", title="Unknown result", state="UNKNOWN_RESULT", data={}),
                Vacancy(source="test", site="", external_id="unknown", url="https://example.test/unknown", title="Unknown", state="UNKNOWN", data={}),
            ]
        )
        db.commit()

    rejected = client.get("/api/vacancies", params={"state": "REJECTED_BY_MODEL"})
    assert rejected.status_code == 200
    assert {item["state"] for item in rejected.json()["items"]} == {"REJECTED_BY_MODEL", "FILTERED_OUT"}

    errors = client.get("/api/vacancies", params={"state": "ERROR"})
    assert errors.status_code == 200
    assert {item["state"] for item in errors.json()["items"]} == {"ERROR", "FAILED", "UNKNOWN", "UNKNOWN_RESULT"}

    exact = client.get("/api/vacancies", params={"state": "SUBMITTED"})
    assert exact.status_code == 200
    assert {item["state"] for item in exact.json()["items"]} == {"SUBMITTED"}


@pytest.mark.parametrize(
    "sort",
    [
        "id",
        "title",
        "state",
        "date",
        "site",
        "total_score",
        "tasks",
        "skills",
        "experience_depth",
        "role_match",
        "industry",
        "special_requirements",
    ],
)
def test_every_vacancy_sort_is_supported_and_missing_scores_stay_last(vacancy_api, sort):
    client, _, ids = vacancy_api
    response = client.get("/api/vacancies", params={"sort": sort, "sort_dir": "desc"})
    assert response.status_code == 200
    returned = [item["id"] for item in response.json()["items"]]
    assert sorted(returned) == sorted(ids.values())
    if sort in {"total_score", *api.VACANCY_SCORE_KEYS}:
        assert returned[-1] == ids["missing"]

    ascending = client.get("/api/vacancies", params={"sort": sort, "sort_dir": "asc"})
    assert ascending.status_code == 200
    if sort in {"total_score", *api.VACANCY_SCORE_KEYS}:
        assert ascending.json()["items"][-1]["id"] == ids["missing"]


def test_invalid_vacancy_sort_direction_and_export_format_are_rejected(vacancy_api):
    client, _, _ = vacancy_api
    assert client.get("/api/vacancies", params={"sort": "unsafe"}).status_code == 422
    assert client.get("/api/vacancies", params={"sort_dir": "sideways"}).status_code == 422
    assert client.get("/api/vacancies/export", params={"format": "pdf"}).status_code == 422


@pytest.mark.parametrize("format", ["csv", "xlsx", "xml"])
def test_exports_share_filters_sorting_and_exact_twelve_columns(vacancy_api, format):
    client, _, ids = vacancy_api
    response = client.get(
        "/api/vacancies/export",
        params={
            "format": format,
            "site": "HH.ru",
            "total_score_min": 80,
            "tasks_min": 30,
            "sort": "tasks",
            "sort_dir": "asc",
        },
    )
    assert response.status_code == 200
    assert f'filename="vacancies.{format}"' in response.headers["content-disposition"]

    if format == "csv":
        parsed = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        headers, values = parsed
    elif format == "xlsx":
        workbook = load_workbook(io.BytesIO(response.content), read_only=True, data_only=True)
        headers, values = list(workbook.active.values)
        headers = list(headers)
        values = list(values)
    else:
        root = ET.fromstring(response.content)
        fields = root.findall("./vacancy/field")
        headers = [field.attrib["name"] for field in fields]
        values = [field.text or "" for field in fields]

    assert headers == list(api.VACANCY_EXPORT_HEADERS)
    assert len(values) == 12
    assert str(values[0]) == str(ids["current"])
    assert values[1] == "Product manager"
    assert values[3] == "HH.ru"
    expected_date = datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc).astimezone().strftime(
        "%d.%m.%Y %H:%M"
    )
    assert values[4] == expected_date
    assert str(values[5]) == "80"
    assert str(values[6]) == "30"


def test_new_vacancies_get_platform_and_state_changes_get_a_new_timestamp(vacancy_api):
    _, session_factory, _ = vacancy_api
    with session_factory() as db:
        vacancy = Vacancy(
            source="hh",
            external_id="new-platform",
            url="https://example.test/new",
            title="New",
            state="DISCOVERED",
            data={},
        )
        explicit_legacy = Vacancy(
            source="hh",
            site="",
            external_id="explicit-legacy",
            url="https://example.test/explicit-legacy",
            title="Explicit legacy",
            state="DISCOVERED",
            data={},
        )
        db.add_all([vacancy, explicit_legacy])
        db.commit()
        assert vacancy.site == "HH.ru"
        assert explicit_legacy.site == ""
        assert vacancy.status_changed_at is not None

        vacancy.status_changed_at = datetime.now() - timedelta(days=1)
        old_timestamp = vacancy.status_changed_at
        vacancy.state = "EVALUATING"
        db.commit()
        assert api._comparable_status_time(vacancy.status_changed_at) > old_timestamp
