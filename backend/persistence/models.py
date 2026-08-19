from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

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
    # Legacy columns remain nullable so old sessions and reports can be read
    # during the migration window. New API code does not use them as a resume.
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resume_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
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


class SearchPolicy(Base):
    __tablename__ = "search_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    suitable_text: Mapped[str] = mapped_column(Text, default="")
    excluded_text: Mapped[str] = mapped_column(Text, default="")
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    compiled: Mapped[dict] = mapped_column(JSON, default=dict)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class SiteAccount(Base):
    __tablename__ = "site_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[str] = mapped_column(String(50), unique=True)
    login_state: Mapped[str] = mapped_column(String(40), default="unknown")


class JobSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    # Kept only so historical sessions remain readable. New runtime sessions
    # do not load or evaluate search policies.
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_policies.id"), nullable=True
    )
    score_threshold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=str(RELEVANCE_SCORE_THRESHOLD),
    )
    adapter_id: Mapped[str] = mapped_column(String(50))
    mode: Mapped[str] = mapped_column(String(40), default="analysis_only")
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


class Vacancy(Base):
    __tablename__ = "vacancies"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
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


class ReviewItem(Base):
    __tablename__ = "review_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    vacancy_id: Mapped[int | None] = mapped_column(ForeignKey("vacancies.id"))
    kind: Mapped[str] = mapped_column(String(80))
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    answer: Mapped[str | None] = mapped_column(Text)


class BrowserEvent(Base):
    __tablename__ = "browser_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    event_type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), unique=True)
    summary: Mapped[dict] = mapped_column(JSON)
    html_path: Mapped[str] = mapped_column(String(500))
    json_path: Mapped[str] = mapped_column(String(500))
    csv_path: Mapped[str] = mapped_column(String(500))
    pdf_path: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
