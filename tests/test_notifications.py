import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.api.router import mark_all_notifications_read, mark_notification_read, notifications
from backend.persistence.database import Base
from backend.persistence.models import CandidateProfile, JobSession, Notification, Vacancy


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_session_status_notifications_are_atomic_and_skip_login_resume():
    Sessions = _db()
    with Sessions() as db:
        profile = CandidateProfile()
        db.add(profile)
        db.flush()
        item = JobSession(profile_id=profile.id, adapter_id="hh", status="CREATED", counters={})
        db.add(item)
        db.commit()
        item.status = "RUNNING"
        db.commit()
        item.status = "WAITING_FOR_LOGIN"
        db.commit()
        item.status = "RUNNING"
        db.commit()
        rows = db.scalars(select(Notification)).all()
        assert [(row.kind, row.message) for row in rows] == [("session_started", f"Сессия {item.id} запущена"), ("session_status_changed", f"Сессия {item.id}: статус изменён на WAITING_FOR_LOGIN")]


def test_creating_session_does_not_create_notification():
    Sessions = _db()
    with Sessions() as db:
        profile = CandidateProfile()
        db.add(profile)
        db.flush()
        db.add(JobSession(profile_id=profile.id, adapter_id="hh", status="CREATED", counters={}))
        db.commit()
        assert db.scalar(select(Notification.id)) is None


@pytest.mark.parametrize(("old", "new", "expected"), [
    ("PAUSED", "RUNNING", True),
    ("RUNNING", "RUNNING", False),
])
def test_status_notification_matrix(old, new, expected):
    Sessions = _db()
    with Sessions() as db:
        profile = CandidateProfile(); db.add(profile); db.flush()
        item = JobSession(profile_id=profile.id, adapter_id="hh", status=old, counters={})
        db.add(item); db.commit()
        item.status = new; db.commit()
        assert (db.scalar(select(Notification.id)) is not None) is expected


def test_status_notification_rollback_is_atomic():
    Sessions = _db()
    with Sessions() as db:
        profile = CandidateProfile(); db.add(profile); db.flush()
        item = JobSession(profile_id=profile.id, adapter_id="hh", status="CREATED", counters={})
        db.add(item); db.commit()
        item.status = "RUNNING"
        db.flush()
        assert db.scalar(select(Notification.id)) is not None
        db.rollback()
        fresh = db.get(JobSession, item.id)
        assert fresh.status == "CREATED"
        assert db.scalar(select(Notification.id)) is None


@pytest.mark.parametrize("state", ["ERROR", "UNKNOWN", "NEEDS_REVIEW"])
def test_vacancy_insert_notification_contract(state):
    Sessions = _db()
    with Sessions() as db:
        vacancy = Vacancy(source="hh", external_id="1", url="https://example.test/1",
                          title="Python разработчик", company="Acme", state=state, data={})
        db.add(vacancy)
        db.commit()
        row = db.scalar(select(Notification).where(Notification.source_type == "vacancy"))
        assert row.source_id == str(vacancy.id)
        assert row.target_path == "/vacancies"
        assert row.kind == f"vacancy_{state.lower()}"
        assert row.title == "Вакансия: Python разработчик"
        assert "Python разработчик" in row.message
        assert "Acme" in row.message
        assert state in row.message


def test_vacancy_transitions_notify_once_and_skip_non_target_states():
    Sessions = _db()
    with Sessions() as db:
        vacancy = Vacancy(source="hh", external_id="1", url="u", title="T", state="DISCOVERED", data={})
        db.add(vacancy); db.commit()
        vacancy.state = "ERROR"; db.flush(); db.flush(); db.commit()
        vacancy.state = "ERROR"; db.commit()
        vacancy.state = "EXTRACTED"; db.commit()
        vacancy.state = "UNKNOWN"; db.commit()
        assert db.scalar(select(Notification).where(Notification.source_type == "vacancy").order_by(Notification.id.desc())).kind == "vacancy_unknown"
        assert len(db.scalars(select(Notification).where(Notification.source_type == "vacancy")).all()) == 2


def test_questionnaire_review_notification_includes_the_missing_information():
    Sessions = _db()
    with Sessions() as db:
        vacancy = Vacancy(source="hh", external_id="q", url="u", title="T", state="SUBMITTING", data={})
        db.add(vacancy); db.commit()
        vacancy.data = {"application_review_reasons": ["Зарплата: не указан формат работы"]}
        vacancy.state = "NEEDS_REVIEW"
        db.commit()
        row = db.scalar(select(Notification).where(Notification.source_type == "vacancy"))
        assert "Зарплата: не указан формат работы" in row.message


def test_vacancy_insert_flush_then_same_state_no_duplicate():
    Sessions = _db()
    with Sessions() as db:
        vacancy = Vacancy(source="hh", external_id="1", url="u", title="T", state="ERROR", data={})
        db.add(vacancy); db.flush(); vacancy.state = "ERROR"; db.flush(); db.commit()
        assert len(db.scalars(select(Notification).where(Notification.source_type == "vacancy")).all()) == 1


def test_vacancy_notification_rollback_is_atomic():
    Sessions = _db()
    with Sessions() as db:
        vacancy = Vacancy(source="hh", external_id="1", url="u", title="T", state="DISCOVERED", data={})
        db.add(vacancy); db.commit()
        vacancy.state = "ERROR"; db.flush(); db.rollback()
        assert db.scalar(select(Notification).where(Notification.source_type == "vacancy")) is None
        assert db.get(Vacancy, vacancy.id).state == "DISCOVERED"


def test_notification_api_list_read_one_read_all_and_404():
    Sessions = _db()
    with Sessions() as db:
        db.add_all([
            Notification(source_type="session", source_id="1", target_path="/session", kind="a", title="A", message="A"),
            Notification(source_type="session", source_id="2", target_path="/session", kind="b", title="B", message="B"),
        ])
        db.commit()
        rows = notifications(limit=50, db=db)
        assert [row["id"] for row in rows] == sorted([row["id"] for row in rows], reverse=True)
        with pytest.raises(HTTPException) as error:
            mark_notification_read(999, db=db)
        assert error.value.status_code == 404
        mark_notification_read(rows[0]["id"], db=db)
        assert db.get(Notification, rows[0]["id"]).read_at is not None
        result = mark_all_notifications_read(db=db)
        assert result["updated"] == 1
        assert all(row.read_at is not None for row in db.scalars(select(Notification)).all())
