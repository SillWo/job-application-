"""Store the confirmed grammatical-gender preference per saved source."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("saved_resume_sources")
    }
    if "grammatical_gender" not in columns:
        with op.batch_alter_table("saved_resume_sources") as batch:
            batch.add_column(sa.Column("grammatical_gender", sa.String(length=10), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("saved_resume_sources")
    }
    if "grammatical_gender" in columns:
        with op.batch_alter_table("saved_resume_sources") as batch:
            batch.drop_column("grammatical_gender")
