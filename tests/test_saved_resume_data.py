from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.persistence.database import Base
from backend.persistence.models import SavedResumeSource
from backend.schemas.domain import SiteResumeSnapshot
from backend.services.private_text import ResumeImportError
from backend.services.resume_session import (
    _normalize_extracted,
    _redacted_snapshot,
    _seal_private,
    _sha256,
    _snapshot_hash,
    confirm_saved_resume_source,
    issue_preview_token,
    load_saved_resume_data,
    refresh_saved_resume_source,
    saved_resume_source_record,
    update_saved_resume_gender,
)

URL = "https://hh.ru/resume/abc123"


def _snapshot(*, name: str = "Synthetic Candidate", experience: str = "Original work"):
    snapshot = _normalize_extracted(
        {
            "source_resume_id": "abc123",
            "identity": {"full_name": name, "gender": "male"},
            "contacts": {"email": "candidate@example.test"},
            "target": {"desired_title": "Engineer"},
            "experience": [{"company": "Example Co", "position": "Engineer", "duties": experience}],
        },
        adapter_id="hh",
        source_url=URL,
    )
    return snapshot


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'saved-resume.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _confirm(db: Session, snapshot=None, *, gender="female"):
    snapshot = snapshot or _snapshot()
    token = issue_preview_token(db, "hh", snapshot, source_url=URL)
    row, _ = confirm_saved_resume_source(
        db,
        adapter_id="hh",
        preview_token=token,
        consent=True,
        grammatical_gender=gender,
    )
    return row


def test_confirm_persists_full_sealed_snapshot_and_loads_after_new_session(db, tmp_path):
    row = _confirm(db)
    payload = row.resume_snapshot_payload
    assert payload and "Synthetic Candidate" not in payload
    row_id = row.id
    db.commit()

    with Session(db.get_bind()) as reopened:
        durable = reopened.scalar(select(SavedResumeSource).where(SavedResumeSource.id == row_id))
        loaded = load_saved_resume_data(durable)
        assert loaded.identity.full_name.value == "Synthetic Candidate"
        assert loaded.contacts.email.value == "candidate@example.test"
        assert loaded.identity.gender.value == "female"
        assert loaded.content_hash == _snapshot_hash(loaded)
        assert saved_resume_source_record(durable)["resume_data_status"] == "ready"


def test_loader_rejects_missing_corrupt_hash_mismatch_and_non_local_site(db):
    row = _confirm(db)
    row.resume_snapshot_payload = None
    with pytest.raises(ResumeImportError, match="Обновите данные резюме во вкладке «Профиль»"):
        load_saved_resume_data(row)
    assert saved_resume_source_record(row)["resume_data_status"] == "missing"

    row = _confirm(db, _snapshot(name="Second Candidate"))
    row.resume_snapshot_payload = "sealed-test:broken"
    with pytest.raises(ResumeImportError, match="Обновите данные резюме во вкладке «Профиль»"):
        load_saved_resume_data(row)
    assert saved_resume_source_record(row)["resume_data_status"] == "corrupt"

    row = _confirm(db, _snapshot(name="Third Candidate"))
    row.resume_id_hash = _sha256("another-resume")
    with pytest.raises(ResumeImportError):
        load_saved_resume_data(row)

    row = _confirm(db, _snapshot(name="Fourth Candidate"))
    row.adapter_id = "hirehi"
    with pytest.raises(ResumeImportError):
        load_saved_resume_data(row)


def test_gender_change_reseals_snapshot_without_losing_full_content(db):
    row = _confirm(db, _snapshot(experience="Long synthetic experience"))
    before = load_saved_resume_data(row)
    imported_at = row.resume_data_saved_at
    updated = update_saved_resume_gender(db, "hh", "male")
    after = load_saved_resume_data(updated)
    assert after.identity.gender.value == "male"
    duties = after.experience[0].duties
    assert (duties.value if hasattr(duties, "value") else duties) == "Long synthetic experience"
    assert before.experience == after.experience
    assert after.content_hash == _snapshot_hash(after)
    assert updated.resume_data_saved_at == imported_at


def test_inconsistent_saved_gender_is_corrupt_and_requests_profile_update(db):
    row = _confirm(db)
    row.grammatical_gender = "male"
    with pytest.raises(ResumeImportError, match="Обновите данные резюме во вкладке «Профиль»"):
        load_saved_resume_data(row)
    record = saved_resume_source_record(row)
    assert record["resume_data_status"] == "corrupt"
    assert "Обновите данные резюме во вкладке «Профиль»" in record["resume_data_error_message"]


@pytest.mark.asyncio
async def test_refresh_replaces_snapshot_and_failed_refresh_preserves_last_good_copy(db, monkeypatch):
    row = _confirm(db, _snapshot(experience="Before refresh"))
    refreshed = _snapshot(name="Updated Candidate", experience="After refresh")

    async def read_updated(_db, _adapter_id):
        return row, URL, refreshed

    monkeypatch.setattr("backend.services.resume_session._read_saved_resume_source", read_updated)
    updated, token = await refresh_saved_resume_source(db, "hh", issue_token=False)
    assert token is None
    assert load_saved_resume_data(updated).experience[0].duties == "After refresh"
    good_payload = updated.resume_snapshot_payload

    async def failed_read(_db, _adapter_id):
        raise RuntimeError("synthetic site failure")

    monkeypatch.setattr("backend.services.resume_session._read_saved_resume_source", failed_read)
    failed, token = await refresh_saved_resume_source(db, "hh", issue_token=False)
    assert token is None
    assert failed.status == "unavailable"
    assert failed.resume_snapshot_payload == good_payload
    assert load_saved_resume_data(failed).identity.full_name.value == "Updated Candidate"


def test_loader_rejects_semantic_hash_and_coverage_tampering(db):
    row = _confirm(db)
    payload = load_saved_resume_data(row).model_dump(mode="json")
    payload["content_hash"] = "0" * 64
    row.resume_snapshot_payload = _seal_private(payload)
    with pytest.raises(ResumeImportError):
        load_saved_resume_data(row)


def test_loader_rejects_hidden_field_even_when_coverage_and_hashes_are_consistent(db):
    row = _confirm(db)
    payload = load_saved_resume_data(row).model_dump(mode="json")
    payload["identity"]["age"] = {"value": None, "availability": "hidden"}
    payload["coverage"] = {
        "present_sections": [],
        "missing_sections": [],
        "hidden_fields": [],
        "unsupported_fields": [],
        "parse_errors": [],
    }
    snapshot = SiteResumeSnapshot.model_validate(payload)
    snapshot = snapshot.model_copy(update={"content_hash": _snapshot_hash(snapshot)})
    public_snapshot, _ = _redacted_snapshot(snapshot)
    row.content_hash = public_snapshot.content_hash
    row.resume_snapshot_payload = _seal_private(snapshot.model_dump(mode="json"))

    assert not snapshot.coverage.hidden_fields
    assert snapshot.content_hash == _snapshot_hash(snapshot)
    assert row.content_hash == _redacted_snapshot(snapshot)[0].content_hash
    with pytest.raises(ResumeImportError, match="Обновите данные резюме во вкладке «Профиль»"):
        load_saved_resume_data(row)


def test_loader_rejects_coverage_hidden_marker(db):
    row = _confirm(db, _snapshot(name="Coverage Candidate"))
    payload = load_saved_resume_data(row).model_dump(mode="json")
    payload["coverage"]["hidden_fields"] = ["experience"]
    payload["content_hash"] = _snapshot_hash(SiteResumeSnapshot.model_validate(payload))
    row.resume_snapshot_payload = _seal_private(payload)
    with pytest.raises(ResumeImportError):
        load_saved_resume_data(row)


def test_confirming_replacement_replaces_full_snapshot_for_same_site(db):
    first = _confirm(db, _snapshot(name="First Candidate"))
    first_id = first.id
    second = _confirm(db, _snapshot(name="Replacement Candidate", experience="Replacement text"))
    assert second.id == first_id
    loaded = load_saved_resume_data(second)
    assert loaded.identity.full_name.value == "Replacement Candidate"
    assert loaded.experience[0].duties == "Replacement text"


def test_hirehi_confirmation_keeps_legacy_no_durable_snapshot_contract(db):
    snapshot = _normalize_extracted(
        {
            "source_resume_id": "hhid",
            "identity": {"full_name": "HireHi Candidate", "gender": "male"},
            "target": {"desired_title": "Engineer"},
            "experience": [{"company": "Example Co", "position": "Engineer"}],
        },
        adapter_id="hirehi",
        source_url="https://hirehi.ru/resume/hhid",
    )
    token = issue_preview_token(db, "hirehi", snapshot, source_url="https://hirehi.ru/resume/hhid")
    row, _ = confirm_saved_resume_source(
        db, adapter_id="hirehi", preview_token=token, consent=True, grammatical_gender="male"
    )
    assert row.resume_snapshot_payload is None
    assert row.resume_data_saved_at is None
    record = saved_resume_source_record(row)
    assert record["uses_saved_data"] is False
    assert record["resume_data_status"] is None


def test_api_record_does_not_expose_snapshot_payload_or_personal_values(db):
    row = _confirm(db)
    record = saved_resume_source_record(row)
    serialized = repr(record)
    assert "resume_snapshot_payload" not in record
    assert "Synthetic Candidate" not in serialized
    assert "candidate@example.test" not in serialized
    assert record["uses_saved_data"] is True
    assert record["resume_data_saved_at"]


def _migration_config(path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_migration_adds_nullable_snapshot_columns_and_keeps_existing_rows_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("JAO_DATABASE_URL", raising=False)
    empty = tmp_path / "empty.db"
    command.upgrade(_migration_config(empty), "head")
    with sqlite3.connect(empty) as connection:
        columns = {row[1] for row in connection.execute("pragma table_info(saved_resume_sources)")}
        assert {"resume_snapshot_payload", "resume_data_saved_at"} <= columns

    filled = tmp_path / "filled.db"
    config = _migration_config(filled)
    command.upgrade(config, "0043")
    with sqlite3.connect(filled) as connection:
        connection.execute(
            "insert into saved_resume_sources "
            "(adapter_id, source_url, source_url_hash, resume_id_hash, content_hash, preview, status, changed) "
            "values ('hh', ?, ?, ?, ?, '{}', 'valid', 0)",
            (URL, _sha256(URL), _sha256("abc123"), "0" * 64),
        )
        connection.commit()
    command.upgrade(config, "head")
    with sqlite3.connect(filled) as connection:
        assert connection.execute(
            "select resume_snapshot_payload, resume_data_saved_at from saved_resume_sources"
        ).fetchone() == (None, None)
