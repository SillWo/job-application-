"""Repair a missing runtime worker start timestamp column."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "session_execution" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("session_execution")}
    if "worker_started_at" not in columns:
        op.add_column(
            "session_execution",
            sa.Column("worker_started_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    # This column logically belongs to 0040. Keeping it avoids breaking fresh
    # databases where 0040 created it and makes downgrade safe across schemas.
    pass
