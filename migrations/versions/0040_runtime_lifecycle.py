"""Durable runtime intent, idempotency and site-worker leases."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    # Terminal session snapshots are immutable recovery inputs. Neutralize
    # legacy CREATED-style TTLs before the runtime starts pruning rows.
    if "session_resume_snapshots" in existing and "sessions" in existing:
        op.execute(
            "UPDATE session_resume_snapshots SET expires_at = NULL "
            "WHERE session_id IN (SELECT id FROM sessions "
            "WHERE status IN ('COMPLETED', 'STOPPED', 'FAILED'))"
        )
    if "session_execution" not in existing:
        op.create_table(
            "session_execution",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("stage", sa.String(64), nullable=False, server_default="PREPARING"),
            sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("wait_reason", sa.String(255)),
            sa.Column("next_retry_at", sa.DateTime(timezone=True)),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("start_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_url", sa.Text()),
            sa.Column("source_url_hash", sa.String(64)),
            sa.Column("source_content_hash", sa.String(64)),
            sa.Column("worker_pid", sa.Integer()),
            sa.Column("worker_started_at", sa.DateTime(timezone=True)),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
            sa.Column("error", sa.Text()),
            sa.UniqueConstraint("session_id", name="uq_session_execution_session"),
        )
        op.create_index("ix_session_execution_session_id", "session_execution", ["session_id"])
    elif "worker_started_at" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("session_execution")
    }:
        op.add_column("session_execution", sa.Column("worker_started_at", sa.DateTime(timezone=True)))
    if "session_idempotency_keys" not in existing:
        op.create_table(
            "session_idempotency_keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("scope", sa.String(120), nullable=False, server_default="sessions"),
            sa.Column("idempotency_key", sa.String(255), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("scope", "idempotency_key", name="uq_session_idempotency_scope_key"),
        )
        op.create_index("ix_session_idempotency_keys_session_id", "session_idempotency_keys", ["session_id"])
    if "site_execution_leases" not in existing:
        op.create_table(
            "site_execution_leases",
            sa.Column("site_id", sa.String(50), primary_key=True),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    # Keep this migration recoverable for fresh/test databases.  Production
    # callers never run downgrade against a live worker queue.
    for name in ("site_execution_leases", "session_idempotency_keys", "session_execution"):
        op.drop_table(name)
