from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
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
    # HireHi-only paid discovery tools. Keep the launch choice auditable.
    hirehi_pro_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


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
    resume link, not a credential. The full normalized snapshot for local
    resume sites is stored only in the protected payload; hashes detect source
    replacement and bind that payload to this row.
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
    # Full normalized snapshot protected by DPAPI (or the explicit test envelope).
    # Kept separate from the redacted preview and never exposed by API records.
    resume_snapshot_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_data_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    search_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    title_sort: Mapped[str] = mapped_column(Text, default="", server_default="")
    site_sort: Mapped[str] = mapped_column(Text, default="", server_default="")
    error_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "source",
            "external_id",
            name="uq_vacancies_session_source_external_id",
        ),
        Index("ix_vacancies_projection_search", "search_text"),
        # SQLite can scan a mixed-direction index without a temp sort for the
        # API's stable ``value DESC, id ASC`` order.  A plain ASC composite
        # index cannot satisfy that order because reversing the scan also
        # reverses the id tie-breaker.
        Index("ix_vacancies_projection_title", title_sort.desc(), id.asc()),
        Index("ix_vacancies_projection_site", site_sort.desc(), id.asc()),
        Index("ix_vacancies_projection_state", state.desc(), id.asc()),
        Index("ix_vacancies_projection_date", status_changed_at.desc(), id.asc()),
        Index("ix_vacancies_projection_title_asc", title_sort.asc(), id.asc()),
        Index("ix_vacancies_projection_site_asc", site_sort.asc(), id.asc()),
        Index("ix_vacancies_projection_state_asc", state.asc(), id.asc()),
        Index("ix_vacancies_projection_date_asc", status_changed_at.asc(), id.asc()),
    )


@event.listens_for(Vacancy, "before_insert")
def _set_vacancy_site(mapper, connection, item: Vacancy) -> None:
    if item.site is None:
        item.site = {"hh": "HH.ru", "hirehi": "HireHi", "zarplata": "Zarplata.ru"}.get(item.source, item.source or "")


def _sync_vacancy_projection(item: Vacancy) -> None:
    data = item.data if isinstance(item.data, dict) else {}
    title = item.title or ""
    company = item.company or ""
    site = item.site or ""
    item.search_text = " ".join(str(value) for value in (item.external_id or "", title, company) if value).casefold()
    item.title_sort = title.casefold()
    item.site_sort = site.casefold()
    item.error_code = data.get("error_code") or data.get("outcome_code")
    item.error_message = data.get("error_message") or data.get("outcome_message")


@event.listens_for(Vacancy, "before_insert")
def _sync_vacancy_projection_insert(mapper, connection, item: Vacancy) -> None:
    _sync_vacancy_projection(item)


@event.listens_for(Vacancy, "before_update")
def _sync_vacancy_projection_update(mapper, connection, item: Vacancy) -> None:
    _sync_vacancy_projection(item)


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
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tasks: Mapped[float | None] = mapped_column(Float, nullable=True)
    skills: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_depth: Mapped[float | None] = mapped_column(Float, nullable=True)
    role_match: Mapped[float | None] = mapped_column(Float, nullable=True)
    industry: Mapped[float | None] = mapped_column(Float, nullable=True)
    special_requirements: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    __table_args__ = (
        Index("ix_evaluations_projection_total", total_score.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_tasks", tasks.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_skills", skills.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_experience_depth", experience_depth.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_role_match", role_match.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_industry", industry.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_special_requirements", special_requirements.desc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_total_asc", total_score.asc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_tasks_asc", tasks.asc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_skills_asc", skills.asc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_experience_depth_asc", experience_depth.asc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_role_match_asc", role_match.asc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_industry_asc", industry.asc(), vacancy_id.asc()),
        Index("ix_evaluations_projection_special_requirements_asc", special_requirements.asc(), vacancy_id.asc()),
    )


def _numeric(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _sync_evaluation_projection(item: Evaluation) -> None:
    data = item.data if isinstance(item.data, dict) else {}
    values = {"total_score": _numeric(data.get("score"))}
    values.update({key: None for key in ("tasks", "skills", "experience_depth", "role_match", "industry", "special_requirements")})
    breakdown = data.get("score_breakdown")
    if isinstance(breakdown, list):
        rows = ((row.get("key"), row.get("points")) for row in breakdown if isinstance(row, dict))
    elif isinstance(breakdown, dict):
        rows = breakdown.items()
    else:
        rows = ()
    aliases = {"required_years": "experience_depth", "title": "role_match", "languages": "special_requirements"}
    canonical_values = {}
    legacy_values = {}
    for raw_key, raw_value in rows:
        if not isinstance(raw_key, str):
            continue
        key = aliases.get(raw_key)
        if key is not None:
            legacy_values[key] = _numeric(raw_value)
        elif raw_key in values and raw_key != "total_score":
            canonical_values[raw_key] = _numeric(raw_value)
    for key in values:
        if key == "total_score":
            continue
        values[key] = canonical_values.get(key, legacy_values.get(key))
    for key, value in values.items():
        setattr(item, key, value)
    item.decision = data.get("decision") if isinstance(data.get("decision"), str) else None
    item.confidence = _numeric(data.get("confidence"))
    item.category = data.get("category") if isinstance(data.get("category"), str) else None


@event.listens_for(Evaluation, "before_insert")
def _sync_evaluation_projection_insert(mapper, connection, item: Evaluation) -> None:
    _sync_evaluation_projection(item)


@event.listens_for(Evaluation, "before_update")
def _sync_evaluation_projection_update(mapper, connection, item: Evaluation) -> None:
    _sync_evaluation_projection(item)


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
