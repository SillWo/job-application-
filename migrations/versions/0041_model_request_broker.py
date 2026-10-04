"""Durable, fair model-request broker queue and session-local cache."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("diagnostic_id", sa.String(36), nullable=False),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(50), nullable=False),
        sa.Column("vacancy_id", sa.String(255)),
        sa.Column("stage", sa.String(100), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("schema_ref", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("model_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.String(100), nullable=False),
        sa.Column("parser_version", sa.String(100), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("canonical_input", sa.Text(), nullable=False),
        sa.Column("canonical_output", sa.Text()),
        sa.Column(
            "cache_source_request_id",
            sa.String(36),
            sa.ForeignKey("model_requests.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("error_code", sa.String(100)),
        sa.CheckConstraint(
            "status IN ('queued','running','retry','completed','failed','cancelled')",
            name="ck_model_requests_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_model_requests_attempt_nonnegative"),
        sa.CheckConstraint(
            "attempt <= max_attempts", name="ck_model_requests_attempt_within_limit"
        ),
        sa.CheckConstraint("generation >= 0", name="ck_model_requests_generation_nonnegative"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_model_requests_max_attempts_positive"),
        sa.CheckConstraint(
            "deadline_at > created_at", name="ck_model_requests_deadline_after_create"
        ),
        sa.CheckConstraint(
            "status != 'running' OR (started_at IS NOT NULL AND lease_owner IS NOT NULL)",
            name="ck_model_requests_running_lease",
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed','failed','cancelled') OR completed_at IS NOT NULL",
            name="ck_model_requests_terminal_completed_at",
        ),
        sa.CheckConstraint(
            "status != 'completed' OR canonical_output IS NOT NULL",
            name="ck_model_requests_completed_output",
        ),
        sa.UniqueConstraint("diagnostic_id", name="uq_model_requests_diagnostic_id"),
    )
    op.create_index(
        "ix_model_requests_schedule",
        "model_requests",
        ["status", "available_at", "created_at", "id"],
    )
    op.create_index(
        "ix_model_requests_site_fifo",
        "model_requests",
        ["site_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_model_requests_session_status",
        "model_requests",
        ["session_id", "status"],
    )
    op.create_index(
        "ix_model_requests_stale",
        "model_requests",
        ["status", "heartbeat_at", "started_at"],
    )

    op.create_table(
        "model_response_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column(
            "source_request_id",
            sa.String(36),
            sa.ForeignKey("model_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_output", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "cache_key", name="uq_model_cache_session_key"),
    )
    op.create_index(
        "ix_model_cache_session_created",
        "model_response_cache",
        ["session_id", "created_at"],
    )

    op.create_table(
        "model_generation_health",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_success_request_id",
            sa.String(36),
            sa.ForeignKey("model_requests.id", ondelete="SET NULL"),
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_failure_request_id",
            sa.String(36),
            sa.ForeignKey("model_requests.id", ondelete="SET NULL"),
        ),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(100)),
        sa.CheckConstraint("id = 1", name="ck_model_generation_health_singleton"),
    )


def downgrade() -> None:
    op.drop_table("model_generation_health")
    op.drop_index("ix_model_cache_session_created", table_name="model_response_cache")
    op.drop_table("model_response_cache")
    op.drop_index("ix_model_requests_stale", table_name="model_requests")
    op.drop_index("ix_model_requests_session_status", table_name="model_requests")
    op.drop_index("ix_model_requests_site_fifo", table_name="model_requests")
    op.drop_index("ix_model_requests_schedule", table_name="model_requests")
    op.drop_table("model_requests")
