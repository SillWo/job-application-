import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.api.router import SessionCreate, session_dict
from backend.persistence.database import Base
from backend.persistence.models import CandidateProfile, JobSession
from backend.schemas.domain import SessionStatus


def _payload(**overrides):
    return {"profile_id": 1, "adapter_id": "hh", **overrides}


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
        profile_id=1,
        adapter_id="hh",
        cover_letter_auto=False,
        cover_letter_template="Шаблон [ФИО]",
    )
    result = session_dict(item)
    assert result["cover_letter_auto"] is False
    assert result["cover_letter_template"] == "Шаблон [ФИО]"
    assert result["cover_letter_max_words"] is None


def test_preference_policy_is_persisted_but_hidden_from_session_payload():
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description="GameDev", preference_policy={"red": []})
    result = session_dict(item)
    assert result["desired_job_description"] == "GameDev"
    assert "preference_policy" not in result


def test_session_model_has_preference_columns():
    assert "desired_job_description" in JobSession.__table__.c
    assert "preference_policy" in JobSession.__table__.c


def test_session_create_requires_profile_gender(monkeypatch, db):
    profile = CandidateProfile(gender=None)
    db.add(profile)
    db.commit()
    monkeypatch.setattr(api.adapter_registry, "get", lambda _: object())
    payload = SessionCreate.model_validate(_payload(profile_id=profile.id))
    with pytest.raises(HTTPException) as exc_info:
        api.create_session(payload, db)
    assert exc_info.value.status_code == 422
    assert "пол" in str(exc_info.value.detail)









@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "GameDev"])
async def test_start_accepts_session_without_waiting_for_model(monkeypatch, db, description):
    db.add(CandidateProfile(id=1, gender="male"))
    db.flush()
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description=description,
                      status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    called = []

    async def unavailable(self):
        raise AssertionError("The HTTP start endpoint must not depend on model health")

    monkeypatch.setattr(api.ModelGateway, "status", unavailable)
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: called.append("launch") or True)
    assert await api.start_session(item.id, db) == {"ok": True}
    assert called == ["launch"]
    assert db.get(JobSession, item.id).status == SessionStatus.RUNNING
    assert item.preference_policy is None  # the durable workflow compiles it


@pytest.mark.asyncio
async def test_start_rejects_legacy_profile_without_gender(monkeypatch, db):
    profile = CandidateProfile(gender=None)
    db.add(profile)
    db.flush()
    item = JobSession(profile_id=profile.id, adapter_id="hh", status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    called = []
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: called.append("launch") or True)
    with pytest.raises(HTTPException) as exc_info:
        await api.start_session(item.id, db)
    assert exc_info.value.status_code == 422
    assert called == []


def test_public_vacancies_hide_flag_matches(monkeypatch):
    assert api.public_evaluation({"score": 80, "flag_matches": [{"confidence": 1}]} ) == {"score": 80}
