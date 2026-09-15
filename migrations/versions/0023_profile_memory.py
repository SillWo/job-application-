"""Private shared profile memory, post-session questions and optional completion mode."""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
    with op.batch_alter_table("sessions") as batch:
        if "guaranteed_application" not in columns:
            batch.add_column(sa.Column("guaranteed_application", sa.Boolean(), nullable=False, server_default="0"))
        if "questions_collected_at" not in columns:
            batch.add_column(sa.Column("questions_collected_at", sa.DateTime(timezone=True), nullable=True))
    metadata = sa.MetaData()
    sa.Table(
        "candidate_profiles", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    sa.Table(
        "sessions", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    sa.Table(
        "vacancies", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    sa.Table(
        "profile_memory", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("candidate_profiles.id"), nullable=False),
        sa.Column("memory_key", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("profile_id", "memory_key"),
    ).create(bind, checkfirst=True)
    sa.Table(
        "session_questions", metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("candidate_profiles.id"), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id"), nullable=True),
        sa.Column("memory_key", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "memory_key"),
    ).create(bind, checkfirst=True)


def downgrade() -> None:
    op.drop_table("session_questions")
    op.drop_table("profile_memory")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("questions_collected_at")
        batch.drop_column("guaranteed_application")
