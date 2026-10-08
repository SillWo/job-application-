from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.router import router
from backend.persistence.database import Base, get_db
from backend.persistence.execution_models import SessionExecution
from backend.persistence.models import JobSession, SavedResumeSource
from backend.services.resume_session import (
    _normalize_extracted,
    full_resume_model_payload,
    persist_session_snapshot,
)


def _snapshot():
    return _normalize_extracted(
        {
            "external_id": "abcdef1234567890",
            "identity": {"full_name": "Ada Lovelace", "gender": "female"},
            "contacts": {
                "email": "ada@frozen.example",
                "phone": "+7 900 000-00-00",
                "messengers": ["https://t.me/ada-frozen"],
            },
            "target": {"title": "Principal Python engineer"},
            "experience": [{"company": "Analytical Engines", "title": "Engineer"}],
            "skills": [{"name": "Python"}],
            "coverage": {
                "present_sections": ["identity", "contacts", "experience", "skills"],
                "missing_sections": ["education"],
            },
        },
        adapter_id="hh",
        source_url="https://hh.ru/resume/abcdef1234567890",
    )


def _client():
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
    return TestClient(app), factory


def _seed(factory):
    frozen = _snapshot()
    with factory() as db:
        session = JobSession(
            adapter_id="hh",
            status="COMPLETED",
            counters={},
            desired_job_description="historical role",
        )
        db.add(session)
        db.flush()
        persist_session_snapshot(db, session.id, frozen)
        db.add(
            SessionExecution(
                session_id=session.id,
                stage="COMPLETED",
                source_url="https://hh.ru/resume/abcdef1234567890",
                source_url_hash=frozen.source_url_hash,
                source_content_hash=frozen.content_hash,
                stage_started_at=datetime.now(timezone.utc),
                last_progress_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            SavedResumeSource(
                adapter_id="hh",
                source_url="https://hh.ru/resume/0123456789abcdef",
                source_url_hash="1" * 64,
                resume_id_hash="2" * 64,
                content_hash="3" * 64,
                preview={},
            )
        )
        db.commit()
        return session.id, frozen


def test_historical_resume_is_frozen_and_safe_after_saved_source_changes():
    client, factory = _client()
    session_id, frozen = _seed(factory)
    try:
        with factory() as db:
            source = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == "hh"))
            source.source_url = "https://hh.ru/resume/1111111111111111"
            db.commit()

        response = client.get(f"/api/sessions/{session_id}/resume")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["import_url"] == "https://hh.ru/resume/abcdef1234567890?print=true"
        assert body["snapshot"]["contacts"]["email"]["value"] == "ada@frozen.example"
        assert body["snapshot"]["coverage"]["missing_sections"] == ["education"]
        assert body["private_fields_found"] == {"full_name": True, "phone": True, "email": True}
        assert "private_view" not in body and "ciphertext" not in response.text
        assert body["snapshot"]["source_resume_id"] == frozen.source_resume_id
    finally:
        client.close()


def test_ai_context_is_exact_frozen_full_model_payload_after_source_deletion():
    client, factory = _client()
    session_id, frozen = _seed(factory)
    try:
        with factory() as db:
            source = db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == "hh"))
            db.delete(source)
            db.commit()

        response = client.get(f"/api/sessions/{session_id}/ai-context")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["resume"] == full_resume_model_payload(body["snapshot"])
        assert body["resume"]["identity"]["full_name"] == "Ada Lovelace"
        assert body["resume"]["contacts"]["email"] == "ada@frozen.example"
        assert body["resume"]["contacts"]["phone"] == "+7 900 000-00-00"
        assert "coverage" not in body["resume"]
        assert "availability" not in str(body["resume"])
        assert "source_section" not in str(body["resume"])
        assert body["snapshot"]["coverage"]["missing_sections"] == ["education"]
        assert "private_view" not in body and "ciphertext" not in response.text
    finally:
        client.close()


def test_resume_snapshot_and_ai_context_return_404_for_missing_session_or_snapshot():
    client, factory = _client()
    try:
        assert client.get("/api/sessions/404/resume").status_code == 404
        assert client.get("/api/sessions/404/ai-context").status_code == 404
        with factory() as db:
            item = JobSession(adapter_id="hh", status="COMPLETED", counters={})
            db.add(item)
            db.commit()
            session_id = item.id
        assert client.get(f"/api/sessions/{session_id}/resume").status_code == 404
        assert client.get(f"/api/sessions/{session_id}/ai-context").status_code == 404
    finally:
        client.close()
