"""Fix the default relevance score threshold for new sessions.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "score_threshold",
            existing_type=sa.Integer(),
            existing_nullable=False,
            existing_server_default="75",
            server_default="70",
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "score_threshold",
            existing_type=sa.Integer(),
            existing_nullable=False,
            existing_server_default="70",
            server_default="75",
        )
