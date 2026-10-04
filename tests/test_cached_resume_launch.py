"""Session launch uses an immutable local HH/Zarplata resume copy."""

from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.api import router as api
from backend.persistence.database import Base, get_db
from backend.persistence.execution_models import SiteExecutionLease
from backend.persistence.models import JobSession, SavedResumeSource, SessionResumeSnapshot
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import _normalize_extracted, _redacted_snapshot, _seal_private


@pytest.fixture
def cached_launch_client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'cached-launch.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    starts = []
    monkeypatch.setattr(
        api, "runtime_supervisor",
        type("Supervisor", (), {"start": lambda _self, **kwargs: starts.append(kwargs) or object()})(),
    )

    def database():
        with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, sessions, starts
    engine.dispose()


def _saved_source(adapter_id: str, *, cached: bool = True, payload: str | None = None):
    host = "hh.ru" if adapter_id == "hh" else "zarplata.ru"
    resume_id = f"cached-{adapter_id}-fixture"
    url = f"https://{host}/resume/{resume_id}"
    snapshot = _normalize_extracted(
        {
            "external_id": resume_id,
            "identity": {"full_name": "Synthetic Candidate", "gender": "male"},
            "contacts": {"email": "candidate@example.test"},
            "target": {"title": "Engineer"},
            "about": f"Saved {adapter_id} profile data",
            "skills": [{"name": "Python"}],
        },
        adapter_id=adapter_id,
        source_url=url,
    )
    public, _ = _redacted_snapshot(snapshot)
    source = SavedResumeSource(
        adapter_id=adapter_id,
        source_url=url,
        source_url_hash=hashlib.sha256(url.encode()).hexdigest(),
        resume_id_hash=hashlib.sha256(resume_id.encode()).hexdigest(),
        content_hash=public.content_hash,
        resume_snapshot_payload=(payload if payload is not None else
                                  (_seal_private(snapshot.model_dump(mode="json")) if cached else None)),
        preview={},
        status="unavailable",
    )
    return source, snapshot


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata"])
def test_create_session_uses_cached_snapshot_without_network_and_pins_it(
    cached_launch_client, monkeypatch, adapter_id
):
    client, sessions, starts = cached_launch_client
    source, _ = _saved_source(adapter_id)
    with sessions() as db:
        db.add(source)
        db.commit()
    monkeypatch.setattr(api, "extract_resume", lambda *_a, **_k: pytest.fail("launch opened site"))

    response = client.post("/api/sessions", json={"adapter_id": adapter_id, "auto_start": True})

    assert response.status_code == 202, response.text
    session_id = response.json()["id"]
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id))
        assert snapshot is not None
        assert snapshot.full_snapshot["source_site"] == adapter_id
        assert snapshot.full_snapshot["target"]["desired_title"]["value"] == "Engineer"
        assert snapshot.full_snapshot["about"]["value"] == f"Saved {adapter_id} profile data"
        assert db.scalar(select(JobSession).where(JobSession.id == session_id)).status == SessionStatus.PREPARING
        # Changing or removing the profile row cannot change the session copy.
        db.delete(source)
        db.commit()
        assert db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)) is not None
    assert starts == [{"site_id": adapter_id, "session_id": session_id}]


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata"])
@pytest.mark.parametrize("source_kind", ["missing", "corrupt", "url_only"])
def test_create_rejects_unusable_cached_source_before_creating_session_or_lease(
    cached_launch_client, adapter_id, source_kind
):
    client, sessions, starts = cached_launch_client
    if source_kind != "missing":
        source, _ = _saved_source(
            adapter_id,
            cached=source_kind != "url_only",
            payload="sealed-test:damaged" if source_kind == "corrupt" else None,
        )
        with sessions() as db:
            db.add(source)
            db.commit()

    response = client.post("/api/sessions", json={"adapter_id": adapter_id})

    assert response.status_code == 400
    assert "Обновите данные резюме во вкладке «Профиль»" in response.json()["detail"]
    with sessions() as db:
        assert db.scalar(select(func.count(JobSession.id))) == 0
        assert db.scalar(select(func.count(SiteExecutionLease.site_id))) == 0
        assert db.scalar(select(func.count(SessionResumeSnapshot.session_id))) == 0
    assert starts == []


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata"])
def test_legacy_created_session_starts_from_local_snapshot_without_network(
    cached_launch_client, monkeypatch, adapter_id
):
    client, sessions, starts = cached_launch_client
    source, _ = _saved_source(adapter_id)
    with sessions() as db:
        db.add(source)
        item = JobSession(adapter_id=adapter_id, status=SessionStatus.CREATED, counters={})
        db.add(item)
        db.commit()
        session_id = item.id
    monkeypatch.setattr(api, "extract_resume", lambda *_a, **_k: pytest.fail("start opened site"))

    response = client.post(f"/api/sessions/{session_id}/start")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == SessionStatus.PREPARING
    with sessions() as db:
        snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id))
        assert snapshot is not None
        assert snapshot.full_snapshot["source_site"] == adapter_id
    assert starts == [{"site_id": adapter_id, "session_id": session_id}]


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata"])
@pytest.mark.parametrize("source_kind", ["missing", "corrupt"])
def test_legacy_created_session_rejects_unusable_local_snapshot_before_start(
    cached_launch_client, adapter_id, source_kind
):
    client, sessions, starts = cached_launch_client
    with sessions() as db:
        if source_kind == "corrupt":
            source, _ = _saved_source(adapter_id, payload="sealed-test:damaged")
            db.add(source)
        item = JobSession(adapter_id=adapter_id, status=SessionStatus.CREATED, counters={})
        db.add(item)
        db.commit()
        session_id = item.id

    response = client.post(f"/api/sessions/{session_id}/start")

    assert response.status_code == 422
    assert "Обновите данные резюме во вкладке «Профиль»" in response.json()["detail"]
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.CREATED
        assert db.scalar(select(func.count(SessionResumeSnapshot.session_id))) == 0
        assert db.scalar(select(func.count(SiteExecutionLease.site_id))) == 0
    assert starts == []
