"""Persist the launch form independently of browser storage."""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("session_form_draft"):
        op.create_table(
            "session_form_draft",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("draft", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("session_form_draft")
