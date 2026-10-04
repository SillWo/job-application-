"""Durable state used by the runtime supervisor.

These tables deliberately live beside, rather than inside, the legacy
workflow JSON.  A worker can therefore be replaced or the API restarted
without losing the launch intent, cancellation fence, or generation token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionExecution(Base):
    __tablename__ = "session_execution"
    __table_args__ = (UniqueConstraint("session_id", name="uq_session_execution_session"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False, default="PREPARING")
    stage_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_progress_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    wait_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    start_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Immutable source fence.  A worker receives the session id and reads the
    # snapshot/source through its own DB connection.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionIdempotencyKey(Base):
    __tablename__ = "session_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_session_idempotency_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(120), nullable=False, default="sessions")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SiteExecutionLease(Base):
    """One durable browser/workflow owner per adapter site."""

    __tablename__ = "site_execution_leases"

    site_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Explicit aliases make the contract readable to callers while retaining one
# canonical SQLAlchemy mapping for migrations and imports.
ExecutionMetadata = SessionExecution
ExecutionLease = SiteExecutionLease
