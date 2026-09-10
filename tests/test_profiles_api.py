import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.api.router import router
from backend.persistence.database import get_db
from backend.persistence.models import CandidateProfile
from backend.schemas.domain import CandidateProfileData, CandidateProfileInput


@pytest.mark.parametrize("payload", [{"gender": "male"}, {
    "full_name": "Fixture Candidate",
    "residence": "Fixture City",
    "job_search_locations": ["Fixture City", "Remote"],
    "contacts": {"email": "fixture@example.test", "messengers": ["fixture"]},
    "education": [{"type": "school", "institution": "Fixture School"}],
    "languages": [{"language": "English", "proficiency": "B2"}],
    "driver_license": False,
    "gender": "female",
}])
def test_create_profile_on_fresh_migrated_database(tmp_path, monkeypatch, payload):
    monkeypatch.delenv("JAO_DATABASE_URL", raising=False)
    url = f"sqlite:///{(tmp_path / 'profiles.db').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url, connect_args={"check_same_thread": False})

    def database():
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app) as client:
            response = client.post("/api/profiles", json=payload)
            assert response.status_code == 200
            created = response.json()
            expected = CandidateProfileData.model_validate(payload).model_dump()
            assert created["data"] == expected
            assert created["resumes"] == []
            assert client.get(f"/api/profiles/{created['id']}").json() == created
            assert client.get("/api/profiles").json() == [created]
        with Session(engine) as db:
            stored = db.get(CandidateProfile, created["id"])
            for key, value in expected.items():
                assert getattr(stored, key) == value
    finally:
        engine.dispose()


def test_profile_write_schema_requires_explicit_gender():
    with pytest.raises(ValidationError):
        CandidateProfileInput.model_validate({})
    assert CandidateProfileInput.model_validate({"gender": "female"}).gender == "female"
