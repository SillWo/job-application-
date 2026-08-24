"""Persist the single user model connection."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("ai_model_settings"):
        return
    op.create_table(
        "ai_model_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ai_model_settings_singleton"),
    )


def downgrade():
    if sa.inspect(op.get_bind()).has_table("ai_model_settings"):
        op.drop_table("ai_model_settings")
