"""Durable vacancy-pipeline state owned by the orchestrator.

The rows in this module are coordination metadata only.  Resume/profile
content remains in the immutable session snapshot and model inputs remain in
the broker tables; duplicating either here would widen the trust boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

PIPELINE_STAGES = (
    "discovery",
    "extraction",
    "evaluation",
    "letter",
    "submission",
    "reporting",
    "completed",
)
PIPELINE_STATUSES = ("queued", "running", "completed", "failed", "cancelled")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PipelineItem(Base):
    """One idempotent vacancy journey for one runtime generation."""

    __tablename__ = "pipeline_items"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('discovery','extraction','evaluation','letter','submission','reporting','completed')",
            name="ck_pipeline_items_stage",
        ),
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_pipeline_items_status",
        ),
        CheckConstraint("generation >= 0", name="ck_pipeline_items_generation_nonnegative"),
        UniqueConstraint(
            "session_id",
            "site_id",
            "external_id",
            "generation",
            name="uq_pipeline_item_identity",
        ),
        Index("ix_pipeline_items_site_queue", "site_id", "stage", "status", "created_at", "id"),
        Index("ix_pipeline_items_session_queue", "session_id", "generation", "status", "stage"),
        Index("ix_pipeline_items_vacancy", "vacancy_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    vacancy_id: Mapped[int | None] = mapped_column(
        ForeignKey("vacancies.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="discovery")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    diagnostic_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PipelineCheckpoint(Base):
    """Session/site cursor and overflow backlog outside the active queue."""

    __tablename__ = "pipeline_checkpoints"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_pipeline_checkpoints_generation_nonnegative"),
        UniqueConstraint(
            "session_id", "site_id", "name", "generation",
            name="uq_pipeline_checkpoint_identity",
        ),
        Index("ix_pipeline_checkpoints_session", "session_id", "generation", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PipelineModelOperation(Base):
    """Idempotency link between a pipeline stage and its durable broker row."""

    __tablename__ = "pipeline_model_operations"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_pipeline_model_ops_generation_nonnegative"),
        CheckConstraint(
            "status IN ('submitting','queued','running','retry','completed','failed','cancelled')",
            name="ck_pipeline_model_ops_status",
        ),
        UniqueConstraint(
            "session_id",
            "site_id",
            "vacancy_key",
            "stage",
            "role",
            "input_hash",
            "versions_hash",
            "generation",
            name="uq_pipeline_model_operation",
        ),
        Index("ix_pipeline_model_ops_request", "request_id"),
        Index("ix_pipeline_model_ops_session", "session_id", "generation", "stage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_items.id", ondelete="CASCADE"), nullable=True
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(String(50), nullable=False)
    vacancy_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    versions_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    diagnostic_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
