import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api.session_draft import router
from backend.persistence.database import Base, get_db
from backend.persistence.models import JobSession


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)

    def database():
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_round_trip_without_browser_and_stale_tab_protection(client):
    browser, _ = client
    assert browser.get('/api/session-draft').json() == {"revision": 0, "draft": None}
    payload = {"revision": 0, "draft": {"desiredJobDescription": "Исследования\nНе продажи", "applicationLimit": "", "unlimitedApplications": True}}
    saved = browser.put('/api/session-draft', json=payload)
    assert saved.status_code == 200
    assert saved.json()['revision'] == 1
    assert browser.get('/api/session-draft').json() == saved.json()
    assert browser.put('/api/session-draft', json=payload).status_code == 409
    saved = browser.put('/api/session-draft', json={"revision": 1, "draft": {"desiredJobDescription": "Новое"}})
    assert saved.json()['revision'] == 2
    assert browser.put('/api/session-draft', json={"revision": 1, "draft": {}}).status_code == 409
    assert browser.get('/api/session-draft').json()['draft']['desiredJobDescription'] == 'Новое'


def test_recovers_latest_description_but_respects_saved_empty_text(client):
    browser, engine = client
    with Session(engine) as db:
        db.add_all([
            JobSession(profile_id=1, adapter_id='hh', desired_job_description='Первое'),
            JobSession(profile_id=1, adapter_id='hirehi', desired_job_description='Последнее', application_limit=None),
            JobSession(profile_id=1, adapter_id='hh', desired_job_description=''),
        ])
        db.commit()
    restored = browser.get('/api/session-draft').json()
    assert restored['draft']['desiredJobDescription'] == 'Последнее'
    assert restored['draft']['adapter'] == 'hirehi'
    assert restored['draft']['unlimitedApplications'] is True
    assert browser.put('/api/session-draft', json={"revision": 0, "draft": {"desiredJobDescription": ""}}).status_code == 200
    assert browser.get('/api/session-draft').json()['draft']['desiredJobDescription'] == ''


@pytest.mark.parametrize('draft', [
    {"desiredJobDescription": "x" * 2001}, {"adapter": "unknown"},
    {"cookies": "not form data"}, {"influence": {"tasks": "invalid"}},
])
def test_rejects_invalid_or_unrelated_data(client, draft):
    browser, _ = client
    assert browser.put('/api/session-draft', json={"revision": 0, "draft": draft}).status_code == 422
