"""Store user vacancy preferences and compiled policy per session."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("desired_job_description", sa.Text(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("preference_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("preference_policy")
        batch_op.drop_column("desired_job_description")
