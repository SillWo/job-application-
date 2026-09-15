from __future__ import annotations

from time import perf_counter

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import backend.services.resume_session as resume_session_service
from backend.intelligence.application_answers import _local_private_answer
from backend.orchestrator.workflow import _snapshot_content_hash, _snapshot_model_payload
from backend.persistence.database import Base
from backend.persistence.models import JobSession, ResumePreviewToken, SessionResumeSnapshot
from backend.schemas.domain import ApplicationField
from backend.services.resume_session import (
    ResumeImportError,
    _normalize_extracted,
    delete_snapshot,
    issue_preview_token,
    persist_session_snapshot,
    private_view,
    professional_model_payload,
    professional_view,
    prune_expired_preview_tokens,
    prune_expired_session_snapshots,
    public_preview,
    render_local_private,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def snapshot():
    return _normalize_extracted(
        {
            "external_id": "abc123",
            "identity": {"full_name": "Ada Lovelace", "gender": "female"},
            "contacts": {"email": "ada@example.test", "phone": "+7 900 000-00-00"},
            "target": {"title": "Python engineer"},
            "skills": [{"name": "Python"}],
        },
        adapter_id="hh",
        source_url="https://hh.ru/resume/abc123",
    )


def test_preview_redacts_bearer_identifiers_and_provenance():
    item = snapshot()
    preview = public_preview(item)
    assert "abc123" not in str(preview)
    assert item.source_url_hash not in str(preview)
    assert item.content_hash not in str(preview)
    assert "Ada Lovelace" not in str(preview)
    assert preview["private_fields_found"] == {"full_name": True, "phone": True, "email": True}
    assert preview["grammatical_gender"] == "female"
    assert preview["grammatical_gender_source"] == "resume"
    serialized = professional_view(item).model_dump(mode="json")
    assert "identity" not in serialized and "contacts" not in serialized
    assert "source_locator" not in str(serialized)
    assert "Ada Lovelace" not in str(serialized)


def test_professional_view_redacts_duplicated_private_values_in_prose_and_links():
    item = _normalize_extracted(
        {
            "external_id": "abc123",
            "identity": {"full_name": "Ada Lovelace", "gender": "female"},
            "contacts": {
                "email": "ada@example.test",
                "phone": "+7 900 000-00-00",
                "messengers": ["https://t.me/ada"],
            },
            "target": {"title": "Python engineer"},
            "about": "Ada Lovelace ada@example.test +7 900 000-00-00",
            "additional_sections": [{"name": "extra", "content": "https://t.me/ada"}],
            "portfolio": [{"title": "Project", "url": "https://t.me/ada"}],
        },
        adapter_id="hh",
        source_url="https://hh.ru/resume/abc123",
    )
    payload = str(professional_view(item).model_dump(mode="json"))
    assert "Ada Lovelace" not in payload
    assert "ada@example.test" not in payload
    assert "+7 900 000-00-00" not in payload
    assert "https://t.me/ada" not in payload


def test_persisted_live_snapshot_hash_matches_redacted_json_and_keeps_professional_data(db):
    session = JobSession(adapter_id="hh", status="CREATED", counters={})
    db.add(session)
    db.flush()

    stored = persist_session_snapshot(db, session.id, snapshot())
    db.commit()
    assert stored.content_hash == stored.snapshot["content_hash"]
    assert _snapshot_content_hash(stored) == stored.content_hash

    model_payload = professional_model_payload(stored.professional_view)
    assert model_payload["target"]["desired_title"] == "Python engineer"
    assert model_payload["skills"][0]["name"] == "Python"
    assert "Ada Lovelace" not in str(model_payload)
    assert "ada@example.test" not in str(model_payload)


def test_legacy_snapshot_without_full_snapshot_restores_complete_model_payload(db):
    session = JobSession(adapter_id="hh", status="CREATED", counters={})
    db.add(session)
    db.flush()
    stored = persist_session_snapshot(db, session.id, snapshot())
    stored.full_snapshot = None
    db.commit()

    payload = _snapshot_model_payload(
        stored, private_view(snapshot()).model_dump(mode="json")
    )
    assert payload["identity"]["full_name"] == "Ada Lovelace"
    assert payload["contacts"]["email"] == "ada@example.test"
    assert payload["skills"][0]["name"] == "Python"


def test_dpapi_failure_fails_closed(monkeypatch, db):
    def fail(_value):
        raise RuntimeError("dpapi unavailable")

    monkeypatch.setattr(resume_session_service, "encrypt_secret", fail)
    monkeypatch.setattr(resume_session_service.sys, "platform", "win32")
    with pytest.raises(ResumeImportError):
        issue_preview_token(db, "hh", snapshot())


def test_local_rendering_has_no_unresolved_markers_or_control_chars():
    item = snapshot()
    private = private_view(item)
    rendered = render_local_private(
        "{{full_name}}\nТелефон: {{phone}}\nПочта: {{email}}\n{{unknown}} [[bad]]\x00",
        private.model_dump(mode="json"),
    )
    assert rendered == "Ada Lovelace\nТелефон: +7 900 000-00-00\nПочта: ada@example.test"
    started = perf_counter()
    for _ in range(1000):
        render_local_private("{{full_name}} {{phone}} {{email}}", private.model_dump(mode="json"))
    assert perf_counter() - started < 0.25


def test_local_form_mapping_uses_private_values_without_model_payload():
    private = private_view(snapshot()).model_dump(mode="json")
    field = ApplicationField(id="email", label="E-mail для связи", kind="text", max_length=100)
    answer = _local_private_answer(field, private)
    assert answer and answer.values == ["ada@example.test"] and answer.source == "local_private"
    assert "ada@example.test" not in str({"fields": [field.model_dump()]})


def test_snapshot_cleanup_is_idempotent(db):
    session = JobSession(adapter_id="hh", status="CREATED", counters={})
    db.add(session)
    db.flush()
    persist_session_snapshot(db, session.id, snapshot())
    db.commit()
    assert delete_snapshot(db, session.id)
    db.commit()
    assert not delete_snapshot(db, session.id)


def test_expiry_pruning_removes_only_stale_preview_rows(db):
    old = issue_preview_token(db, "hh", snapshot())
    current = issue_preview_token(db, "hh", snapshot())
    rows = list(db.scalars(select(ResumePreviewToken).order_by(ResumePreviewToken.token_hash)))
    rows[0].expires_at = rows[0].created_at
    db.flush()
    assert prune_expired_preview_tokens(db) == 1
    db.commit()
    assert len(list(db.scalars(select(ResumePreviewToken)))) == 1
    assert old != current


def test_abandoned_created_snapshot_expiry_does_not_remove_running_snapshot(db):
    created = JobSession(adapter_id="hh", status="CREATED", counters={})
    running = JobSession(adapter_id="hh", status="RUNNING", counters={})
    db.add_all([created, running])
    db.flush()
    persist_session_snapshot(db, created.id, snapshot())
    persist_session_snapshot(db, running.id, snapshot())
    for item in db.scalars(select(SessionResumeSnapshot)):
        item.expires_at = item.imported_at
    db.flush()
    assert prune_expired_session_snapshots(db) == 1
