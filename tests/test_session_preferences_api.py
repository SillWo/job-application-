import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.api import router as api
from backend.api.router import SessionCreate, session_dict
from backend.persistence.database import Base
from backend.persistence.models import JobSession
from backend.schemas.domain import DesiredJobPolicy, SessionStatus


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






    item = JobSession(profile_id=1, adapter_id="hh")



@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
async def test_start_compiles_before_launch_and_saves_policy(monkeypatch, db):
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description="GameDev", status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    order = []
    policy = DesiredJobPolicy()
    monkeypatch.setattr(api.ModelGateway, "status", _available_status)

    async def compile_policy(*args):
        order.append("compile")
        return policy

    monkeypatch.setattr(api, "compile_preference_policy", compile_policy)
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: order.append("launch") or True)
    await api.start_session(item.id, db)
    assert order == ["compile", "launch"]
    assert db.get(JobSession, item.id).preference_policy == policy.model_dump(mode="json")


@pytest.mark.asyncio
async def test_start_compile_failure_does_not_launch(monkeypatch, db):
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description="GameDev", status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    async def compile_policy(*args):
        raise api.ModelUnavailable("offline")
    launched = []
    monkeypatch.setattr(api.ModelGateway, "status", _available_status)
    monkeypatch.setattr(api, "compile_preference_policy", compile_policy)
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: launched.append(True))
    with pytest.raises(api.HTTPException) as error:
        await api.start_session(item.id, db)
    assert error.value.status_code == 503 and not launched
    assert db.get(JobSession, item.id).status == SessionStatus.CREATED


@pytest.mark.asyncio
async def test_empty_description_skips_compiler(monkeypatch, db):
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description="", status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    async def compile_policy(*args):
        raise AssertionError("compiler must not run")
    monkeypatch.setattr(api.ModelGateway, "status", _available_status)
    monkeypatch.setattr(api, "compile_preference_policy", compile_policy)
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: True)
    await api.start_session(item.id, db)
    assert db.get(JobSession, item.id).preference_policy is None


async def _available_status(self):
    return {"connected": True, "model_available": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "GameDev"])
async def test_start_unavailable_model_keeps_created_and_skips_compiler_and_launch(monkeypatch, db, description):
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description=description, status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    called = []

    async def unavailable(self):
        return {"connected": False, "model_available": False}

    async def compile_policy(*args):
        called.append("compile")

    monkeypatch.setattr(api.ModelGateway, "status", unavailable)
    monkeypatch.setattr(api, "compile_preference_policy", compile_policy)
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: called.append("launch"))
    with pytest.raises(api.HTTPException) as error:
        await api.start_session(item.id, db)
    assert error.value.status_code == 503
    assert error.value.detail == "API модели не доступен"
    assert called == []
    assert db.get(JobSession, item.id).status == SessionStatus.CREATED


@pytest.mark.asyncio
async def test_start_model_status_exception_keeps_created_and_skips_launch(monkeypatch, db):
    item = JobSession(profile_id=1, adapter_id="hh", desired_job_description="GameDev", status=SessionStatus.CREATED)
    db.add(item)
    db.commit()
    monkeypatch.setattr(api.ModelGateway, "status", _status_error)
    monkeypatch.setattr(api.workflow_manager, "launch", lambda _: pytest.fail("must not launch"))
    with pytest.raises(api.HTTPException) as error:
        await api.start_session(item.id, db)
    assert error.value.status_code == 503
    assert error.value.detail == "API модели не доступен"
    assert db.get(JobSession, item.id).status == SessionStatus.CREATED


async def _status_error(self):
    raise RuntimeError("probe failed")


def test_public_vacancies_hide_flag_matches(monkeypatch):
    assert api.public_evaluation({"score": 80, "flag_matches": [{"confidence": 1}]} ) == {"score": 80}
