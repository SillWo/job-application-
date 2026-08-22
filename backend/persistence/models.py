from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.schemas.domain import RELEVANCE_SCORE_THRESHOLD

from .database import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    residence: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_search_locations: Mapped[list] = mapped_column(JSON, default=list)
    contacts: Mapped[dict] = mapped_column(JSON, default=dict)
    education: Mapped[list] = mapped_column(JSON, default=list)
    languages: Mapped[list] = mapped_column(JSON, default=list)
    driver_license: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Resume(Base):
    __tablename__ = "resumes"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="Резюме")
    desired_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    desired_salary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employment_types: Mapped[list] = mapped_column(JSON, default=list)
    work_formats: Mapped[list] = mapped_column(JSON, default=list)
    business_trips: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    experiences: Mapped[list] = mapped_column(JSON, default=list)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    about: Mapped[str] = mapped_column(Text, default="")
    selected_for_matching: Mapped[bool] = mapped_column(Boolean, default=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class JobSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    score_threshold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=str(RELEVANCE_SCORE_THRESHOLD),
    )
    minimum_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    adapter_id: Mapped[str] = mapped_column(String(50))
    # These values are a snapshot of the launch configuration.  ``None`` means
    # that the corresponding limit is disabled for this session.
    # API defaults are applied by SessionCreate.  Do not add ORM defaults:
    # SQLAlchemy substitutes them for an explicit None and breaks "unlimited".
    viewed_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    application_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(255))


class Notification(Base):
    """A user-facing event, intentionally generic and independent of sessions."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[str] = mapped_column(String(255), index=True)
    target_path: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


@event.listens_for(Session, "before_flush")
def _notify_session_status_changes(session: Session, _flush_context, _instances) -> None:
    """Create status notifications in the same transaction as the update."""
    for item in session.dirty:
        if not isinstance(item, JobSession):
            continue
        history = inspect(item).attrs.status.history
        if not history.has_changes() or not history.deleted or not history.added:
            continue
        old, new = history.deleted[0], history.added[0]
        if old == new or (old == "WAITING_FOR_LOGIN" and new == "RUNNING"):
            continue
        launch = old == "CREATED" and new == "RUNNING"
        session.add(Notification(
            source_type="session",
            source_id=str(item.id),
            target_path="/session",
            kind="session_started" if launch else "session_status_changed",
            title=f"Сессия {item.id} запущена" if launch else f"Сессия {item.id}: статус изменён",
            message=f"Сессия {item.id} запущена" if launch else f"Сессия {item.id}: статус изменён на {new}",
        ))


class Vacancy(Base):
    __tablename__ = "vacancies"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "source",
            "external_id",
            name="uq_vacancies_session_source_external_id",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    # Historical HH vacancies may outlive their deleted session.
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(40), default="DISCOVERED")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VacancySnapshot(Base):
    __tablename__ = "vacancy_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON)


class ApplicationPlanRecord(Base):
    __tablename__ = "application_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("candidate_profile_id", "vacancy_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"))
    status: Mapped[str] = mapped_column(String(40))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CoverLetter(Base):
    __tablename__ = "cover_letters"
    id: Mapped[int] = mapped_column(primary_key=True)
    vacancy_id: Mapped[int] = mapped_column(ForeignKey("vacancies.id"), unique=True)
    text: Mapped[str] = mapped_column(Text)


class BrowserEvent(Base):
    __tablename__ = "browser_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    event_type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
