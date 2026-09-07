"""Durable recovery metadata and search plan."""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The original baseline creates tables from current ORM metadata.
    if "recovery" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sessions")}:
        return
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("recovery", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("recovery")
