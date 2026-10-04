import hashlib

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.api.router import SessionCreate, session_dict
from backend.persistence.database import Base
from backend.persistence.execution_models import SessionExecution
from backend.persistence.models import JobSession, SavedResumeSource, SessionResumeSnapshot
from backend.schemas.domain import SessionStatus
from backend.services.resume_session import (
    _normalize_extracted,
    _redacted_snapshot,
    _seal_private,
    persist_session_snapshot,
)


def _payload(**overrides):
    return {
        "adapter_id": "hh",
        **overrides,
    }


def test_description_is_limited_to_2000_characters():
    assert len(SessionCreate.model_validate(_payload(desired_job_description="x" * 2000)).desired_job_description) == 2000
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_payload(desired_job_description="x" * 2001))


def test_cover_letter_word_limit_is_optional_with_safe_bounds():
    assert SessionCreate.model_validate(_payload()).cover_letter_max_words == 150
    assert SessionCreate.model_validate(_payload(cover_letter_max_words=240)).cover_letter_max_words == 240
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_payload(cover_letter_max_words=0))
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_payload(cover_letter_max_words=10001))


def test_cover_letter_settings_require_template_only_in_manual_mode():
    automatic = SessionCreate.model_validate(_payload())
    assert automatic.cover_letter_auto is True
    assert automatic.cover_letter_template == ""

    manual = SessionCreate.model_validate(_payload(
        cover_letter_auto=False, cover_letter_template="Я [ФИО] и мой опыт..."
    ))
    assert manual.cover_letter_auto is False
    assert manual.cover_letter_template.startswith("Я [ФИО]")

    with pytest.raises(ValidationError, match="структуру"):
        SessionCreate.model_validate(_payload(cover_letter_auto=False, cover_letter_template=" \n"))


def test_session_dict_exposes_cover_letter_settings():
    item = JobSession(
        adapter_id="hh",
        cover_letter_auto=False,
        cover_letter_template="Шаблон [ФИО]",
    )
    result = session_dict(item)
    assert result["cover_letter_auto"] is False
    assert result["cover_letter_template"] == "Шаблон [ФИО]"
    assert result["cover_letter_max_words"] is None


def test_hirehi_pro_flag_defaults_false_and_is_exposed():
    item = JobSession(adapter_id="hirehi")
    assert SessionCreate.model_validate({"adapter_id": "hirehi"}).hirehi_pro_enabled is False
    assert session_dict(item)["hirehi_pro_enabled"] is False
    item.hirehi_pro_enabled = True
    assert session_dict(item)["hirehi_pro_enabled"] is True


def test_preference_policy_is_persisted_but_hidden_from_session_payload():
    item = JobSession(adapter_id="hh", desired_job_description="GameDev", preference_policy={"red": []})
    result = session_dict(item)
    assert result["desired_job_description"] == "GameDev"
    assert "preference_policy" not in result


def test_session_model_has_preference_columns():
    assert "desired_job_description" in JobSession.__table__.c
    assert "preference_policy" in JobSession.__table__.c


def test_session_create_accepts_durable_source_without_ephemeral_token():
    payload = SessionCreate.model_validate({"adapter_id": "hh"})
    assert payload.adapter_id == "hh"
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"adapter_id": "hh", "resume_preview_token": "x"})


@pytest.mark.parametrize(
    ("adapter_id", "expected_limit", "expected_pro"),
    [("hirehi", None, True), ("hh", 7, False)],
)
def test_create_session_normalizes_hirehi_limit_and_pro_flag(
    monkeypatch, db, adapter_id, expected_limit, expected_pro
):
    source = SavedResumeSource(
        adapter_id=adapter_id,
        source_url_hash="a" * 64,
        resume_id_hash="b" * 64,
        content_hash="c" * 64,
        preview={},
        status="valid",
    )
    if adapter_id == "hh":
        url = "https://hh.ru/resume/preferences-fixture"
        cached = _normalize_extracted(
            {
                "external_id": "preferences-fixture",
                "identity": {"gender": "male"},
                "target": {"title": "Engineer"},
                "about": "Synthetic preferences fixture",
                "skills": [{"name": "Python"}],
            },
            adapter_id="hh",
            source_url=url,
        )
        public, _ = _redacted_snapshot(cached)
        source.source_url = url
        source.source_url_hash = hashlib.sha256(url.encode()).hexdigest()
        source.resume_id_hash = hashlib.sha256(cached.source_resume_id.encode()).hexdigest()
        source.content_hash = public.content_hash
        source.resume_snapshot_payload = _seal_private(cached.model_dump(mode="json"))
    db.add(source)
    db.flush()

    starts = []
    monkeypatch.setattr(
        api, "runtime_supervisor",
        type("Supervisor", (), {"start": lambda _self, **kwargs: starts.append(kwargs) or object()})(),
    )
    payload = SessionCreate.model_validate(
        {"adapter_id": adapter_id, "application_limit": 7, "hirehi_pro_enabled": True,
         "auto_start": True}
    )

    result = api.create_session(payload, db)
    item = db.get(JobSession, result["id"])
    assert item is not None
    assert item.application_limit == expected_limit
    assert item.hirehi_pro_enabled is expected_pro
    assert result["application_limit"] == expected_limit
    assert result["hirehi_pro_enabled"] is expected_pro
    assert result["status"] == SessionStatus.PREPARING
    execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == item.id))
    assert execution is not None and execution.start_requested is True
    assert starts == [{"site_id": adapter_id, "session_id": item.id}]









@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "GameDev"])
async def test_start_accepts_session_without_waiting_for_model(monkeypatch, db, description):
    item = JobSession(adapter_id="hh", desired_job_description=description,
                      status=SessionStatus.CREATED)
    db.add(item)
    db.flush()
    full = _normalize_extracted(
        {
            "external_id": "resume-1",
            "identity": {"gender": "male"},
            "target": {"title": "Engineer"},
            "about": "Synthetic start fixture",
            "skills": [{"name": "Python"}],
        },
        adapter_id="hh",
        source_url="https://hh.ru/resume/resume-1",
    )
    persist_session_snapshot(db, item.id, full)
    db.commit()
    called = []

    async def unavailable(self):
        raise AssertionError("The HTTP start endpoint must not depend on model health")

    monkeypatch.setattr(api.ModelGateway, "status", unavailable)
    monkeypatch.setattr(
        api, "runtime_supervisor",
        type("Supervisor", (), {"start": lambda _self, **kwargs: called.append(kwargs) or object()})(),
    )
    result = await api.start_session(item.id, db)
    assert result["status"] == SessionStatus.PREPARING
    assert called == [{"site_id": "hh", "session_id": item.id}]
    execution = db.scalar(select(SessionExecution).where(SessionExecution.session_id == item.id))
    assert execution is not None and execution.start_requested is True
    assert db.get(JobSession, item.id).status == SessionStatus.PREPARING
    assert item.preference_policy is None  # the durable workflow compiles it


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_data",
    [
        {},
        {"identity": {"gender": {"value": "unknown", "availability": "present"}}},
    ],
    ids=["missing", "invalid"],
)
async def test_start_rejects_snapshot_without_valid_gender(monkeypatch, db, private_data):
    item = JobSession(adapter_id="hh", status=SessionStatus.CREATED)
    db.add(item)
    db.flush()
    db.add(SessionResumeSnapshot(
        session_id=item.id, source_site="hh", source_resume_id="resume-1",
        source_url_hash="a" * 64, content_hash="b" * 64,
        snapshot={}, professional_view={}, private_view=_seal_private(private_data),
    ))
    db.commit()
    called = []
    monkeypatch.setattr(
        api, "runtime_supervisor",
        type("Supervisor", (), {"start": lambda _self, **kwargs: called.append(kwargs) or object()})(),
    )
    with pytest.raises(HTTPException) as exc_info:
        await api.start_session(item.id, db)
    assert exc_info.value.status_code == 422
    assert called == []
    assert db.scalar(
        select(SessionExecution).where(SessionExecution.session_id == item.id)
    ) is None


def test_public_vacancies_hide_flag_matches(monkeypatch):
    assert api.public_evaluation({"score": 80, "flag_matches": [{"confidence": 1}]} ) == {"score": 80}
