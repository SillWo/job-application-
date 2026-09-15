from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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

from .database import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class SessionFormDraft(Base):
    """One shared launch form for this local application, independent of browser origin."""

    __tablename__ = "session_form_draft"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    draft: Mapped[dict] = mapped_column(JSON, nullable=False)


class JobSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    minimum_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    desired_job_description: Mapped[str] = mapped_column(Text, default="")
    preference_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    adapter_id: Mapped[str] = mapped_column(String(50))
    # Application limit is a snapshot of the launch configuration.
    application_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_letter_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    cover_letter_template: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Optional per-session cap for the generated cover letter. ``None`` keeps
    # the writer's backwards-compatible default.
    cover_letter_max_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="CREATED")
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(255))
    recovery: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    guaranteed_application: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class SessionResumeSnapshot(Base):
    """Temporary normalized resume data owned by one session only.

    The JSON columns contain normalized fields, never raw HTML or downloaded
    files.  ``private_view`` is used only for local form/letter assembly;
    ``professional_view`` is the model-facing allowlist.
    """
    __tablename__ = "session_resume_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), unique=True, index=True
    )
    source_site: Mapped[str] = mapped_column(String(50), nullable=False)
    source_resume_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    professional_view: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Complete normalized source captured for the local workflow.  This is
    # optional so rows created before the full-context migration remain
    # readable through ``snapshot`` + sealed ``private_view``.
    full_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # DPAPI-encrypted JSON text retained for local form/letter rendering and
    # backwards compatibility with snapshots created before full_snapshot.
    private_view: Mapped[str] = mapped_column(Text, nullable=False)
    # CREATED sessions may be abandoned before start; their private snapshot
    # has a bounded recovery lifetime. RUNNING/PAUSED snapshots are retained
    # until the normal terminal cleanup path.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResumePreviewToken(Base):
    """Short-lived one-use server-side preview state.

    Only a SHA-256 digest of the bearer token is persisted.  New rows retain
    the canonical public source URL directly.
    Preview responses need not return the URL, and it is never written to
    logs/events.
    """
    __tablename__ = "resume_preview_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String(50), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    professional_view: Mapped[dict] = mapped_column(JSON, nullable=False)
    # New previews retain the complete normalized snapshot for the session
    # created from them.  Older preview rows legitimately have no value.
    full_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # DPAPI-encrypted JSON text retained for local form/letter rendering.
    private_view: Mapped[str] = mapped_column(Text, nullable=False)
    # Canonical public URL for the preview confirmation flow.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SavedResumeSource(Base):
    """Durable, privacy-safe pointer to one confirmed site resume per adapter.

    The canonical public URL is intentionally stored openly: it is a public
    resume link, not a credential.  No resume id, identity, contacts, or private snapshot is stored here; the hashes are
    only used to detect replacement of the external document during a refresh.
    """

    __tablename__ = "saved_resume_sources"
    __table_args__ = (UniqueConstraint("adapter_id", name="uq_saved_resume_sources_adapter"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # User-selected grammatical gender for this confirmed source.  This is a
    # low-sensitivity enum preference, not imported resume content.
    grammatical_gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Canonical public URL for the user-facing profile.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hash only: the external resume id is a bearer-adjacent identifier and is
    # not needed to render or recover a saved source.
    resume_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="valid")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


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
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class AIModelSettings(Base):
    __tablename__ = "ai_model_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ai_model_settings_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


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
        if old == new:
            continue
        launch = old == "CREATED" and new == "RUNNING"
        session.add(
            Notification(
                source_type="session",
                source_id=str(item.id),
                target_path="/session",
                kind="session_started" if launch else "session_status_changed",
                title=f"Сессия {item.id} запущена"
                if launch
                else f"Сессия {item.id}: статус изменён",
                message=f"Сессия {item.id} запущена"
                if launch
                else f"Сессия {item.id}: статус изменён на {new}",
            )
        )


_VACANCY_NOTIFICATION_STATES = {"ERROR"}


def _vacancy_notification(vacancy: Vacancy) -> Notification:
    status = vacancy.state
    company = f" — {vacancy.company}" if vacancy.company else ""
    reasons = (vacancy.data or {}).get("application_error_reasons", [])
    error_message = (vacancy.data or {}).get("error_message")
    detail = "; ".join(reason for reason in reasons[:3] if isinstance(reason, str)) if isinstance(reasons, list) else ""
    if error_message and isinstance(error_message, str):
        detail = error_message if not detail else f"{error_message}; {detail}"
    return Notification(
        source_type="vacancy",
        source_id=str(vacancy.id),
        target_path="/vacancies",
        kind=f"vacancy_{status.lower()}",
        title=f"Вакансия: {vacancy.title}",
        message=f"Вакансия «{vacancy.title}»{company}: статус {status}" + (f". {detail[:1000]}" if detail else ""),
    )


@event.listens_for(Session, "before_flush")
def _notify_vacancy_state_changes(session: Session, _flush_context, _instances) -> None:
    """Notify atomically when a vacancy enters an error terminal state."""
    pending_new = session.info.setdefault("_pending_vacancy_notifications", set())
    for item in session.new:
        if isinstance(item, Vacancy) and item.state in _VACANCY_NOTIFICATION_STATES:
            pending_new.add(id(item))
    for item in session.dirty:
        if not isinstance(item, Vacancy):
            continue
        history = inspect(item).attrs.state.history
        if not history.has_changes() or not history.deleted or not history.added:
            continue
        if history.deleted[0] != history.added[0] and history.added[0] in _VACANCY_NOTIFICATION_STATES:
            session.add(_vacancy_notification(item))


@event.listens_for(Session, "before_flush")
def _stamp_vacancy_state_changes(session: Session, _flush_context, _instances) -> None:
    for item in session.dirty:
        if isinstance(item, Vacancy) and inspect(item).attrs.state.history.has_changes():
            item.status_changed_at = now()


@event.listens_for(Session, "after_flush_postexec")
def _notify_new_vacancy_states(session: Session, _flush_context) -> None:
    pending = session.info.pop("_pending_vacancy_notifications", set())
    for item in session.identity_map.values():
        if isinstance(item, Vacancy) and id(item) in pending:
            session.add(_vacancy_notification(item))


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
    # ``None`` means a newly discovered vacancy whose display platform should
    # be derived from ``source``.  An explicit empty string is reserved for
    # migrated legacy rows and must remain empty.
    site: Mapped[str | None] = mapped_column(String(100), nullable=False, default=None)
    external_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(40), default="EXTRACTED")
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


@event.listens_for(Vacancy, "before_insert")
def _set_vacancy_site(mapper, connection, item: Vacancy) -> None:
    if item.site is None:
        item.site = {"hh": "HH.ru", "hirehi": "HireHi", "zarplata": "Zarplata.ru"}.get(item.source, item.source or "")


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
    __table_args__ = (UniqueConstraint("vacancy_id", name="uq_applications_vacancy_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
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
