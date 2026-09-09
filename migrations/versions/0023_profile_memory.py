"""Private shared profile memory, post-session questions and optional completion mode."""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from backend.persistence.models import ProfileMemory, SessionQuestion

    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
    with op.batch_alter_table("sessions") as batch:
        if "guaranteed_application" not in columns:
            batch.add_column(sa.Column("guaranteed_application", sa.Boolean(), nullable=False, server_default="0"))
        if "questions_collected_at" not in columns:
            batch.add_column(sa.Column("questions_collected_at", sa.DateTime(timezone=True), nullable=True))
    ProfileMemory.__table__.create(bind, checkfirst=True)
    SessionQuestion.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("session_questions")
    op.drop_table("profile_memory")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("questions_collected_at")
        batch.drop_column("guaranteed_application")
