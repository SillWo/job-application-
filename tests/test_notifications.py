import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.api.router import mark_all_notifications_read, mark_notification_read, notifications
from backend.persistence.database import Base
from backend.persistence.models import CandidateProfile, JobSession, Notification


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
