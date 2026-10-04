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


def _saved_source(
    adapter_id: str, *, cached: bool = True, payload: str | None = None,
    name: str = "Synthetic Candidate",
):
    host = {"hh": "hh.ru", "zarplata": "zarplata.ru", "hirehi": "hirehi.ru"}[adapter_id]
    resume_id = f"cached-{adapter_id}-fixture"
    url = f"https://{host}/resume/{resume_id}"
    snapshot = _normalize_extracted(
        {
            "external_id": resume_id,
            "identity": {"full_name": name, "gender": "male"},
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


def _launch_error(adapter_id: str, *, missing: bool) -> str:
    names = {"hh": "HH.ru", "hirehi": "HireHi", "zarplata": "Zarplata.ru"}
    wording = "не загружено резюме" if missing else "не удалось извлечь необходимые данные из резюме"
    return f'Для сайта {names[adapter_id]} {wording}, проверьте раздел "Профиль"'


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata", "hirehi"])
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


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata", "hirehi"])
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
    assert response.json()["detail"] == _launch_error(adapter_id, missing=source_kind == "missing")
    with sessions() as db:
        assert db.scalar(select(func.count(JobSession.id))) == 0
        assert db.scalar(select(func.count(SiteExecutionLease.site_id))) == 0
        assert db.scalar(select(func.count(SessionResumeSnapshot.session_id))) == 0
    assert starts == []


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata", "hirehi"])
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


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata", "hirehi"])
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
    assert response.json()["detail"] == _launch_error(adapter_id, missing=source_kind == "missing")
    with sessions() as db:
        assert db.get(JobSession, session_id).status == SessionStatus.CREATED
        assert db.scalar(select(func.count(SessionResumeSnapshot.session_id))) == 0
        assert db.scalar(select(func.count(SiteExecutionLease.site_id))) == 0
    assert starts == []


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata", "hirehi"])
def test_existing_pinned_session_keeps_its_snapshot_when_profile_copy_changes(
    cached_launch_client, adapter_id
):
    client, sessions, starts = cached_launch_client
    source, original = _saved_source(adapter_id)
    with sessions() as db:
        db.add(source)
        db.commit()

    created = client.post("/api/sessions", json={"adapter_id": adapter_id})
    assert created.status_code == 202, created.text
    session_id = created.json()["id"]
    with sessions() as db:
        session_copy = db.scalar(
            select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
        )
        pinned_hash = session_copy.content_hash
        updated_source, replacement = _saved_source(adapter_id, name="Replacement Candidate")
        source_row = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id))
        source_row.resume_snapshot_payload = updated_source.resume_snapshot_payload
        source_row.content_hash = updated_source.content_hash
        source_row.source_url_hash = updated_source.source_url_hash
        source_row.resume_id_hash = updated_source.resume_id_hash
        source_row.source_url = updated_source.source_url
        db.commit()

    started = client.post(f"/api/sessions/{session_id}/start")
    assert started.status_code == 200, started.text
    with sessions() as db:
        session_copy = db.scalar(
            select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
        )
        assert session_copy.content_hash == pinned_hash
        assert session_copy.full_snapshot["identity"]["full_name"]["value"] == original.identity.full_name.value
        assert session_copy.full_snapshot["identity"]["full_name"]["value"] != replacement.identity.full_name.value
    assert starts == [
        {"site_id": adapter_id, "session_id": session_id},
        {"site_id": adapter_id, "session_id": session_id},
    ]


@pytest.mark.parametrize("adapter_id", ["hh", "zarplata", "hirehi"])
@pytest.mark.parametrize("source_state", ["missing", "corrupt"])
def test_existing_pinned_session_requires_current_usable_profile_before_start(
    cached_launch_client, adapter_id, source_state
):
    client, sessions, starts = cached_launch_client
    source, _ = _saved_source(adapter_id)
    with sessions() as db:
        db.add(source)
        db.commit()

    created = client.post("/api/sessions", json={"adapter_id": adapter_id})
    assert created.status_code == 202, created.text
    session_id = created.json()["id"]
    with sessions() as db:
        pinned = db.scalar(
            select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
        )
        pinned_hash = pinned.content_hash
        lease_count = db.scalar(select(func.count(SiteExecutionLease.site_id)))
        source_row = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == adapter_id))
        if source_state == "missing":
            db.delete(source_row)
        else:
            source_row.resume_snapshot_payload = "sealed-test:damaged"
        db.commit()

    response = client.post(f"/api/sessions/{session_id}/start")
    assert response.status_code == 422
    assert response.json()["detail"] == _launch_error(adapter_id, missing=source_state == "missing")
    with sessions() as db:
        item = db.get(JobSession, session_id)
        pinned = db.scalar(
            select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == session_id)
        )
        assert item.status == SessionStatus.PREPARING
        assert pinned.content_hash == pinned_hash
        assert db.scalar(select(func.count(SiteExecutionLease.site_id))) == lease_count
    assert starts == [{"site_id": adapter_id, "session_id": session_id}]
