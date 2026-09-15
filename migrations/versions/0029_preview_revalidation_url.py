"""Keep a compatibility envelope for preview-token revalidation."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("resume_preview_tokens")}
    if "source_url_encrypted" not in columns:
        with op.batch_alter_table("resume_preview_tokens") as batch:
            batch.add_column(sa.Column("source_url_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("resume_preview_tokens")}
    if "source_url_encrypted" in columns:
        with op.batch_alter_table("resume_preview_tokens") as batch:
            batch.drop_column("source_url_encrypted")
