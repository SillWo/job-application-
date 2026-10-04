from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.router import router
from backend.persistence.database import Base, get_db
from backend.persistence.execution_models import SessionExecution
from backend.persistence.models import JobSession


@pytest.fixture
def history_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, factory


def _seed(factory, count=3, statuses=None):
    with factory() as db:
        for index in range(count):
            status = statuses[index] if statuses is not None else "PREPARING"
            item = JobSession(
                adapter_id="hh",
                desired_job_description=f"role-{index}",
                status=status,
                counters={"viewed": index},
            )
            db.add(item)
            db.flush()
            db.add(SessionExecution(
                session_id=item.id,
                stage="IMPORTING",
                stage_started_at=datetime.now(timezone.utc),
                last_progress_at=datetime.now(timezone.utc),
                heartbeat_at=datetime.now(timezone.utc),
            ))
        db.commit()


def test_empty_history_and_light_legacy_list(history_client):
    client, _ = history_client
    assert client.get("/api/sessions").json() == []
    assert client.get("/api/sessions?legacy=1").json() == []
    response = client.get("/api/sessions/history")
    assert response.status_code == 200
    assert response.json() == {
        "items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False
    }


def test_history_is_paged_and_excludes_recovery(history_client):
    client, factory = history_client
    _seed(factory, 3)
    response = client.get("/api/sessions/history?limit=2&offset=1")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 2 and body["offset"] == 1 and body["has_more"] is False
    assert [item["id"] for item in body["items"]] == [2, 1]
    assert all("recovery" not in item for item in body["items"])
    assert body["items"][0]["execution_stage"] == "IMPORTING"


def test_terminal_history_filters_rows_and_total_before_pagination(history_client):
    client, factory = history_client
    _seed(factory, 6, ["RUNNING", "COMPLETED", "PREPARING", "STOPPED", "FAILED", "CANCELLED"])

    legacy = client.get("/api/sessions/history?limit=2&offset=1").json()
    assert legacy["total"] == 6
    assert [item["id"] for item in legacy["items"]] == [5, 4]
    assert legacy["has_more"] is True

    terminal = client.get(
        "/api/sessions/history?terminal_only=true&limit=2&offset=1"
    ).json()
    assert terminal["total"] == 4
    assert [item["id"] for item in terminal["items"]] == [5, 4]
    assert terminal["has_more"] is True

    final_page = client.get(
        "/api/sessions/history?terminal_only=true&limit=2&offset=3"
    ).json()
    assert final_page["total"] == 4
    assert [item["id"] for item in final_page["items"]] == [2]
    assert final_page["has_more"] is False


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
def test_history_validates_bounds(history_client, query):
    client, _ = history_client
    assert client.get(f"/api/sessions/history?{query}").status_code == 422


def test_get_session_is_no_store(history_client):
    client, factory = history_client
    _seed(factory, 1)
    response = client.get("/api/sessions/1")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
