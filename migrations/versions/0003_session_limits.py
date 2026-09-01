"""store launch limits on sessions

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Server defaults preserve the behaviour of sessions created before this release.
    # Explicit NULL remains the durable representation of unlimited.
    op.add_column(
        "sessions",
        sa.Column("application_limit", sa.Integer(), nullable=True, server_default="5"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "application_limit")
