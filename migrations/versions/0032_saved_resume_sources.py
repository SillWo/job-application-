"""Persist one encrypted, user-confirmed resume link per adapter."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "saved_resume_sources" in inspector.get_table_names():
        return
    op.create_table(
        "saved_resume_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("adapter_id", sa.String(length=50), nullable=False),
        sa.Column("source_url_encrypted", sa.Text(), nullable=False),
        sa.Column("source_url_hash", sa.String(length=64), nullable=False),
        sa.Column("resume_id_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="valid"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.UniqueConstraint("adapter_id", name="uq_saved_resume_sources_adapter"),
    )
    op.create_index(
        "ix_saved_resume_sources_adapter_id",
        "saved_resume_sources",
        ["adapter_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "saved_resume_sources" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_saved_resume_sources_adapter_id", table_name="saved_resume_sources")
    op.drop_table("saved_resume_sources")
