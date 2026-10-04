"""Store protected durable resume snapshots for local resume sites."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "saved_resume_sources" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("saved_resume_sources")}
    if "resume_snapshot_payload" not in columns:
        op.add_column(
            "saved_resume_sources",
            sa.Column("resume_snapshot_payload", sa.Text(), nullable=True),
        )
    if "resume_data_saved_at" not in columns:
        op.add_column(
            "saved_resume_sources",
            sa.Column("resume_data_saved_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "saved_resume_sources" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("saved_resume_sources")}
    if "resume_data_saved_at" in columns:
        op.drop_column("saved_resume_sources", "resume_data_saved_at")
    if "resume_snapshot_payload" in columns:
        op.drop_column("saved_resume_sources", "resume_snapshot_payload")
