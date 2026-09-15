"""Bound the lifetime of abandoned, pre-start session snapshots."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("session_resume_snapshots")}
    if "expires_at" not in columns:
        with op.batch_alter_table("session_resume_snapshots") as batch:
            batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("session_resume_snapshots")}
    if "expires_at" in columns:
        with op.batch_alter_table("session_resume_snapshots") as batch:
            batch.drop_column("expires_at")
