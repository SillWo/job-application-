import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.api.router import SessionCreate, session_dict
from backend.persistence.database import Base
from backend.persistence.models import JobSession
from backend.schemas.domain import SessionStatus


def _payload(**overrides):
    return {"profile_id": 1, "adapter_id": "hh", **overrides}


def test_description_is_limited_to_2000_characters():
    assert len(SessionCreate.model_validate(_payload(desired_job_description="x" * 2000)).desired_job_description) == 2000
    with pytest.raises(ValidationError):
        SessionCreate.model_validate(_payload(desired_job_description="x" * 2001))


def test_preference_policy_is_persisted_but_hidden_from_session_payload():
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description="GameDev", preference_policy={"red": []})
    result = session_dict(item)
    assert result["desired_job_description"] == "GameDev"
    assert "preference_policy" not in result


def test_session_model_has_preference_columns():
    assert "desired_job_description" in JobSession.__table__.c
    assert "preference_policy" in JobSession.__table__.c









@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "GameDev"])
async def test_start_accepts_session_without_waiting_for_model(monkeypatch, db, description):
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


def test_public_vacancies_hide_flag_matches(monkeypatch):
    assert api.public_evaluation({"score": 80, "flag_matches": [{"confidence": 1}]} ) == {"score": 80}
