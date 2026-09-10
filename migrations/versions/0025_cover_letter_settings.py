"""Store profile gender and cover-letter launch settings."""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    profile_columns = {column["name"] for column in sa.inspect(bind).get_columns("candidate_profiles")}
    session_columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
    with op.batch_alter_table("candidate_profiles") as batch:
        if "gender" not in profile_columns:
            batch.add_column(sa.Column("gender", sa.String(length=10), nullable=True))
    with op.batch_alter_table("sessions") as batch:
        if "cover_letter_auto" not in session_columns:
            batch.add_column(sa.Column("cover_letter_auto", sa.Boolean(), nullable=False, server_default="1"))
        if "cover_letter_template" not in session_columns:
            batch.add_column(sa.Column("cover_letter_template", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("cover_letter_template")
        batch.drop_column("cover_letter_auto")
    with op.batch_alter_table("candidate_profiles") as batch:
        batch.drop_column("gender")
