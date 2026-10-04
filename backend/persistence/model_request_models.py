"""Durable model-request queue and session-scoped response cache.

Only canonical, security-sanitized JSON is stored here. Provider credentials,
HTTP headers, browser state and raw exception text are deliberately absent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

MODEL_REQUEST_STATUSES = (
    "queued",
    "running",
    "retry",
    "completed",
    "failed",
    "cancelled",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelRequest(Base):
    """One logical model operation, including all provider/format attempts."""

    __tablename__ = "model_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','retry','completed','failed','cancelled')",
            name="ck_model_requests_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_model_requests_attempt_nonnegative"),
        CheckConstraint("attempt <= max_attempts", name="ck_model_requests_attempt_within_limit"),
        CheckConstraint("generation >= 0", name="ck_model_requests_generation_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_model_requests_max_attempts_positive"),
        CheckConstraint("deadline_at > created_at", name="ck_model_requests_deadline_after_create"),
        CheckConstraint(
            "status != 'running' OR (started_at IS NOT NULL AND lease_owner IS NOT NULL)",
            name="ck_model_requests_running_lease",
        ),
        CheckConstraint(
            "status NOT IN ('completed','failed','cancelled') OR completed_at IS NOT NULL",
            name="ck_model_requests_terminal_completed_at",
        ),
        CheckConstraint(
            "status != 'completed' OR canonical_output IS NOT NULL",
            name="ck_model_requests_completed_output",
        ),
        UniqueConstraint("diagnostic_id", name="uq_model_requests_diagnostic_id"),
        Index("ix_model_requests_schedule", "status", "available_at", "created_at", "id"),
        Index("ix_model_requests_site_fifo", "site_id", "status", "created_at", "id"),
        Index("ix_model_requests_session_status", "session_id", "status"),
        Index("ix_model_requests_stale", "status", "heartbeat_at", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    diagnostic_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(String(50), nullable=False)
    vacancy_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_ref: Mapped[str] = mapped_column(String(500), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_input: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_source_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_requests.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ModelResponseCache(Base):
    """Completed response cache whose uniqueness is explicitly session-local."""

    __tablename__ = "model_response_cache"
    __table_args__ = (
        UniqueConstraint("session_id", "cache_key", name="uq_model_cache_session_key"),
        Index("ix_model_cache_session_created", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_request_id: Mapped[str] = mapped_column(
        ForeignKey("model_requests.id", ondelete="CASCADE"), nullable=False
    )
    canonical_output: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelGenerationHealth(Base):
    """Generation health, intentionally separate from provider catalog discovery."""

    __tablename__ = "model_generation_health"
    __table_args__ = (CheckConstraint("id = 1", name="ck_model_generation_health_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_success_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_requests.id", ondelete="SET NULL"), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_requests.id", ondelete="SET NULL"), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
