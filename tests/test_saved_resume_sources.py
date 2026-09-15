from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import backend.services.resume_session as resume_service
from backend.main import app
from backend.persistence.database import Base, get_db
from backend.persistence.models import (
    JobSession,
    ResumePreviewToken,
    SavedResumeSource,
    SessionResumeSnapshot,
)
from backend.services.resume_session import (
    ResumeImportError,
    _normalize_extracted,
    confirm_saved_resume_source,
    issue_preview_token,
    refresh_saved_resume_source,
    revalidate_saved_resume_source,
    saved_resume_source_record,
)


def _snapshot(title: str = "Python engineer"):
    return _normalize_extracted(
        {
            "external_id": "abc123",
            "identity": {"full_name": "Ada Lovelace", "gender": "female"},
            "contacts": {"email": "ada@example.test", "phone": "+7 900 000-00-00"},
            "target": {"title": title},
            "skills": [{"name": "Python"}],
        },
        adapter_id="hh",
        source_url="https://hh.ru/resume/abc123",
    )


def _snapshot_without_gender(title: str = "Python engineer"):
    return _normalize_extracted(
        {
            "external_id": "abc123",
            "identity": {"full_name": "Ada Lovelace"},
            "target": {"title": title},
            "skills": [{"name": "Python"}],
        },
        adapter_id="hh",
        source_url="https://hh.ru/resume/abc123",
    )


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_confirm_persists_plain_source_and_keeps_token_usable(db):
    token = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    row, returned_token = confirm_saved_resume_source(
        db, adapter_id="hh", preview_token=token, consent=True
    )
    assert returned_token == token
    assert row.source_url == "https://hh.ru/resume/abc123"
    assert "abc123" not in str(row.preview)
    assert "female" not in str(row.preview)
    assert "Ada Lovelace" not in str(row.preview)

    # A new ORM session still sees the durable row, while the original token
    # remains available for the normal session-launch consumption path.
    engine = db.get_bind()
    with Session(engine) as restarted:
        persisted = restarted.scalar(select(SavedResumeSource))
        assert persisted is not None
        assert persisted.adapter_id == "hh"
        assert persisted.source_url == "https://hh.ru/resume/abc123"


def test_confirm_requires_consent_and_upserts_one_row_per_adapter(db):
    first = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    with pytest.raises(ResumeImportError):
        confirm_saved_resume_source(db, adapter_id="hh", preview_token=first, consent=False)
    confirm_saved_resume_source(db, adapter_id="hh", preview_token=first, consent=True)
    second = issue_preview_token(
        db, "hh", _snapshot("Data engineer"), source_url="https://hh.ru/resume/abc123"
    )
    confirm_saved_resume_source(db, adapter_id="hh", preview_token=second, consent=True)
    assert db.scalar(select(SavedResumeSource).where(SavedResumeSource.adapter_id == "hh")).preview[
        "target_title"
    ] == "Data engineer"
    assert len(list(db.scalars(select(SavedResumeSource)))) == 1


def test_confirm_requires_gender_only_when_preview_asks(db):
    token = issue_preview_token(
        db, "hh", _snapshot_without_gender(), source_url="https://hh.ru/resume/abc123"
    )
    with pytest.raises(ResumeImportError, match="Выберите мужской"):
        confirm_saved_resume_source(db, adapter_id="hh", preview_token=token, consent=True)
    row, _ = confirm_saved_resume_source(
        db,
        adapter_id="hh",
        preview_token=token,
        consent=True,
        grammatical_gender="male",
    )
    assert row.grammatical_gender == "male"
    assert not row.preview.get("questions")


@pytest.mark.asyncio
async def test_auto_refresh_marks_changed_and_unavailable_without_losing_row(monkeypatch, db):
    token = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    confirm_saved_resume_source(db, adapter_id="hh", preview_token=token, consent=True)
    monkeypatch.setattr(
        resume_service,
        "validate_adapter_resume_url",
        lambda adapter_id, url: (url, {"external_id": "abc123"}),
    )
    current = _snapshot("Data engineer")

    async def read_current(*args, **kwargs):
        return current

    monkeypatch.setattr(resume_service, "extract_resume", read_current)
    row, fresh_token = await refresh_saved_resume_source(db, "hh")
    assert row.status == "changed"
    assert row.changed is True
    assert fresh_token
    assert not row.preview.get("questions")
    assert saved_resume_source_record(row, preview_token=fresh_token)["preview_token"] == fresh_token

    async def unavailable(*args, **kwargs):
        raise ResumeImportError("page text must not escape")

    monkeypatch.setattr(resume_service, "extract_resume", unavailable)
    row, no_token = await refresh_saved_resume_source(db, "hh")
    assert row.status == "unavailable"
    assert row.error_code == "unavailable"
    assert no_token is None
    assert db.scalar(select(SavedResumeSource)) is not None

    async def missing_answer_read(*args, **kwargs):
        return _snapshot_without_gender()

    monkeypatch.setattr(resume_service, "extract_resume", missing_answer_read)
    row, fresh_token = await refresh_saved_resume_source(db, "hh")
    assert fresh_token
    # The preference saved during confirmation suppresses the old session
    # question even if the current page stops publishing gender.
    assert not row.preview.get("questions")
    assert "grammatical_gender" not in row.preview


@pytest.fixture()
def api_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        yield client, engine
    finally:
        app.dependency_overrides.pop(get_db, None)
        client.close()


def test_api_confirm_validates_consent_and_token_without_consuming(api_client):
    client, engine = api_client
    with Session(engine) as db:
        token = issue_preview_token(
            db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
        )
    response = client.post(
        "/api/resume-sources/confirm",
        json={"adapter_id": "hh", "preview_token": token, "consent": False},
    )
    assert response.status_code == 422
    response = client.post(
        "/api/resume-sources/confirm",
        json={"adapter_id": "hh", "preview_token": "wrong-token-" + "x" * 20, "consent": True},
    )
    assert response.status_code == 422
    response = client.post(
        "/api/resume-sources/confirm",
        json={"adapter_id": "hh", "preview_token": token, "consent": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["adapter_id"] == "hh"
    assert payload["preview_token"] == token
    assert payload["source_url"] == "https://hh.ru/resume/abc123"
    assert "https://hh.ru/resume/abc123" in response.text
    with Session(engine) as db:
        assert db.scalar(select(SavedResumeSource)) is not None


def test_api_gender_preference_persists_and_can_be_updated(api_client):
    client, engine = api_client
    with Session(engine) as db:
        token = issue_preview_token(
            db, "hh", _snapshot_without_gender(), source_url="https://hh.ru/resume/abc123"
        )
    response = client.post(
        "/api/resume-sources/confirm",
        json={
            "adapter_id": "hh",
            "preview_token": token,
            "consent": True,
            "grammatical_gender": "female",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["grammatical_gender"] == "female"
    with Session(engine) as restarted:
        row = restarted.scalar(select(SavedResumeSource))
        assert row is not None and row.grammatical_gender == "female"

    response = client.get("/api/resume-sources")
    assert response.status_code == 200
    assert response.json()[0]["grammatical_gender"] == "female"
    response = client.patch(
        "/api/resume-sources/hh", json={"grammatical_gender": "male"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["grammatical_gender"] == "male"
    with Session(engine) as restarted:
        row = restarted.scalar(select(SavedResumeSource))
        assert row is not None and row.grammatical_gender == "male"


def test_api_refresh_get_delete_contract_and_fresh_token(monkeypatch, api_client):
    client, engine = api_client
    with Session(engine) as db:
        initial = issue_preview_token(
            db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
        )
        confirm_saved_resume_source(
            db, adapter_id="hh", preview_token=initial, consent=True
        )

    monkeypatch.setattr(
        resume_service,
        "validate_adapter_resume_url",
        lambda adapter_id, url: (url, {"external_id": "abc123"}),
    )

    async def fresh_read(*args, **kwargs):
        return _snapshot()

    monkeypatch.setattr(resume_service, "extract_resume", fresh_read)
    response = client.get("/api/resume-sources")
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 1
    record = records[0]
    assert record["adapter_id"] == "hh"
    assert record["status"] == "valid"
    assert "preview_token" not in record
    assert record["source_url"] == "https://hh.ru/resume/abc123"
    assert record["masked_url"] == "https://hh.ru/resume/abc•••"
    # GET is a durable catalog read: it does not open the site or rotate a
    # temporary launch token.
    assert "https://hh.ru/resume/abc123" in response.text

    async def changed_read(*args, **kwargs):
        return _snapshot("Data engineer")

    monkeypatch.setattr(resume_service, "extract_resume", changed_read)
    response = client.post("/api/resume-sources/hh/refresh")
    assert response.status_code == 200
    assert response.json()["status"] == "changed"
    assert response.json().get("preview_token")

    async def unavailable_read(*args, **kwargs):
        raise RuntimeError("untrusted page content")

    monkeypatch.setattr(resume_service, "extract_resume", unavailable_read)
    response = client.post("/api/resume-sources/hh/refresh")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["error_code"] == "unavailable"
    assert response.json()["masked_url"] == "https://hh.ru/resume/abc•••"
    assert "untrusted page content" not in response.text
    response = client.get("/api/resume-sources")
    assert response.json()[0]["status"] == "unavailable"
    assert response.json()[0]["masked_url"] == "https://hh.ru/resume/abc•••"
    assert client.delete("/api/resume-sources/hh").json() == {"ok": True, "adapter_id": "hh"}
    with Session(engine) as db:
        assert db.scalar(select(ResumePreviewToken)) is None
    assert client.post("/api/resume-sources/hh/refresh").status_code == 404


def test_session_create_revalidates_saved_source_without_consuming_preview_token(monkeypatch, api_client):
    client, engine = api_client
    with Session(engine) as db:
        token = issue_preview_token(
            db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
        )
        confirm_saved_resume_source(db, adapter_id="hh", preview_token=token, consent=True)

    monkeypatch.setattr(
        resume_service,
        "validate_adapter_resume_url",
        lambda adapter_id, url: (url, {"external_id": "abc123"}),
    )
    reads = 0

    async def read_current(*args, **kwargs):
        nonlocal reads
        reads += 1
        return _snapshot()

    monkeypatch.setattr(resume_service, "extract_resume", read_current)
    response = client.post(
        "/api/sessions",
        json={
            "adapter_id": "hh",
        },
    )
    assert response.status_code == 200, response.text
    assert reads == 1
    session_id = response.json()["id"]
    monkeypatch.setattr("backend.api.router.workflow_manager.launch", lambda _id: True)
    started = client.post(f"/api/sessions/{session_id}/start")
    assert started.status_code == 200, started.text
    assert reads == 1
    stopped = client.post(f"/api/sessions/{session_id}/stop")
    assert stopped.status_code == 200, stopped.text
    with Session(engine) as db:
        item = db.scalar(select(JobSession).order_by(JobSession.id.desc()))
        assert item is not None
        assert db.scalar(select(SavedResumeSource)) is not None
        assert db.scalar(
            select(ResumePreviewToken).where(ResumePreviewToken.token_hash == resume_service._sha256(token))
        ).consumed_at is None


def test_session_create_uses_saved_gender_when_fresh_resume_has_none(monkeypatch, api_client):
    client, engine = api_client
    with Session(engine) as db:
        token = issue_preview_token(
            db, "hh", _snapshot_without_gender(), source_url="https://hh.ru/resume/abc123"
        )
        confirm_saved_resume_source(
            db,
            adapter_id="hh",
            preview_token=token,
            consent=True,
            grammatical_gender="male",
        )
    monkeypatch.setattr(
        resume_service,
        "validate_adapter_resume_url",
        lambda adapter_id, url: (url, {"external_id": "abc123"}),
    )

    async def read_current(*args, **kwargs):
        return _snapshot_without_gender()

    monkeypatch.setattr(resume_service, "extract_resume", read_current)
    response = client.post("/api/sessions", json={"adapter_id": "hh"})
    assert response.status_code == 200, response.text
    with Session(engine) as db:
        item = db.get(JobSession, response.json()["id"])
        assert item is not None
        snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id))
        assert snapshot.full_snapshot["identity"]["gender"]["value"] == "male"


def test_session_create_prefers_saved_gender_over_fresh_resume(monkeypatch, api_client):
    client, engine = api_client
    with Session(engine) as db:
        token = issue_preview_token(
            db, "hh", _snapshot_without_gender(), source_url="https://hh.ru/resume/abc123"
        )
        confirm_saved_resume_source(
            db,
            adapter_id="hh",
            preview_token=token,
            consent=True,
            grammatical_gender="male",
        )
    monkeypatch.setattr(
        resume_service,
        "validate_adapter_resume_url",
        lambda adapter_id, url: (url, {"external_id": "abc123"}),
    )

    async def read_current(*args, **kwargs):
        return _snapshot()  # The site now publishes a conflicting female value.

    monkeypatch.setattr(resume_service, "extract_resume", read_current)
    response = client.post("/api/sessions", json={"adapter_id": "hh"})
    assert response.status_code == 200, response.text
    with Session(engine) as db:
        item = db.get(JobSession, response.json()["id"])
        assert item is not None
        snapshot = db.scalar(select(SessionResumeSnapshot).where(SessionResumeSnapshot.session_id == item.id))
        assert snapshot.full_snapshot["identity"]["gender"]["value"] == "male"


@pytest.mark.asyncio
async def test_saved_source_survives_session_snapshot_cleanup(db):
    token = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    confirm_saved_resume_source(db, adapter_id="hh", preview_token=token, consent=True)
    session = JobSession(adapter_id="hh", status="CREATED", counters={})
    db.add(session)
    db.flush()
    resume_service.persist_session_snapshot(db, session.id, _snapshot())
    db.commit()

    assert resume_service.delete_snapshot(db, session.id)
    db.commit()
    assert db.scalar(select(SavedResumeSource)) is not None


def test_missing_plain_source_fails_closed_and_is_retained(db):
    token = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    row, _ = confirm_saved_resume_source(
        db, adapter_id="hh", preview_token=token, consent=True
    )
    # A damaged/missing canonical URL fails closed without deleting the source.
    row.source_url = None
    db.commit()
    result, fresh_token = asyncio.run(refresh_saved_resume_source(db, "hh"))
    assert result.status == "unavailable"
    assert fresh_token is None
    assert db.scalar(select(SavedResumeSource)) is not None


@pytest.mark.asyncio
async def test_revalidation_failure_keeps_durable_source_and_public_link(monkeypatch, db):
    token = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    confirm_saved_resume_source(db, adapter_id="hh", preview_token=token, consent=True)

    async def unavailable(*args, **kwargs):
        raise ResumeImportError("page text must not escape")

    monkeypatch.setattr(resume_service, "extract_resume", unavailable)
    with pytest.raises(ResumeImportError):
        await revalidate_saved_resume_source(db, "hh")
    row = db.scalar(select(SavedResumeSource))
    assert row is not None
    assert row.status == "unavailable"
    assert saved_resume_source_record(row)["source_url"] == "https://hh.ru/resume/abc123"
    assert saved_resume_source_record(row)["masked_url"] == "https://hh.ru/resume/abc•••"


@pytest.mark.asyncio
async def test_saved_source_listing_does_not_consume_or_refresh(monkeypatch, db):
    token = issue_preview_token(
        db, "hh", _snapshot(), source_url="https://hh.ru/resume/abc123"
    )
    confirm_saved_resume_source(db, adapter_id="hh", preview_token=token, consent=True)

    async def fail_if_read(*args, **kwargs):
        raise AssertionError("listing must not open the site")

    monkeypatch.setattr(resume_service, "extract_resume", fail_if_read)
    rows = await resume_service.list_saved_resume_sources(db)
    assert len(rows) == 1
    assert rows[0][0].status == "valid"
    assert rows[0][1] is None
    assert db.scalar(select(SavedResumeSource)) is not None


def test_saved_source_migration_upgrades_from_0031(tmp_path):
    path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.upgrade(config, "0031")
    command.upgrade(config, "head")
    with Session(create_engine(f"sqlite:///{path.as_posix()}")) as db:
        assert db.execute(select(SavedResumeSource)).all() == []
