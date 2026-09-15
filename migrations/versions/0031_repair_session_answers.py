"""Repair session_answers for databases upgraded past 0028 without it."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
    if "session_answers" not in columns:
        with op.batch_alter_table("sessions") as batch:
            batch.add_column(
                sa.Column("session_answers", sa.JSON(), nullable=False, server_default="{}")
            )


def downgrade() -> None:
    # This revision only repairs installations that skipped the 0028 column.
    # The column is owned by 0028, so removing it here would make a downgrade
    # destructive for databases where 0028 created it normally.
    pass
