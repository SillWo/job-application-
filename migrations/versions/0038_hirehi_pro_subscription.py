"""Persist the HireHi PRO subscription choice on each session."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("sessions"):
        return
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "hirehi_pro_enabled" in columns:
        return
    # A server default makes this safe for existing rows and keeps inserts
    # from older clients valid.  The ORM also supplies the same default.
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(
            sa.Column(
                "hirehi_pro_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("sessions"):
        return
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "hirehi_pro_enabled" not in columns:
        return
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("hirehi_pro_enabled")
