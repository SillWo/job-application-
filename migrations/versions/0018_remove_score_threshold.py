"""Remove the obsolete aggregate relevance threshold."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("score_threshold")


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column("score_threshold", sa.Integer(), nullable=False, server_default="70")
        )
