import sqlalchemy as sa
from alembic import op

from backend.persistence import models  # noqa: F401
from backend.persistence.database import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Pin the initial schema even when newer ORM models add columns. Without
    # these exclusions a clean `upgrade head` would create columns added by
    # later revisions here and following migrations would attempt to add them
    # again.
    legacy_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(legacy_metadata)
    # These tables/columns are intentionally historical: later migrations
    # remove them, but 0001 must still bootstrap the old schema independently
    # of the current runtime ORM models.
    sessions = legacy_metadata.tables["sessions"]
    vacancies = legacy_metadata.tables["vacancies"]
    for constraint in list(vacancies.constraints):
        if isinstance(constraint, sa.UniqueConstraint):
            vacancies.constraints.remove(constraint)
    vacancies.append_constraint(sa.UniqueConstraint("source", "external_id"))
    sessions.append_column(
        sa.Column("mode", sa.String(40), nullable=True, server_default="analysis_only")
    )
    sa.Table(
        "review_items",
        legacy_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("vacancy_id", sa.Integer, sa.ForeignKey("vacancies.id")),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("answer", sa.Text),
    )
    sa.Table(
        "reports",
        legacy_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "session_id", sa.Integer, sa.ForeignKey("sessions.id"), nullable=False, unique=True
        ),
        sa.Column("summary", sa.JSON, nullable=False),
        sa.Column("html_path", sa.String(500), nullable=False),
        sa.Column("json_path", sa.String(500), nullable=False),
        sa.Column("csv_path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("viewed_limit", "application_limit", "minimum_scores", "resume_url", "resume_path"):
        if column in sessions.c:
            sessions._columns.remove(sessions.c[column])
    if "employer_contacts" in legacy_metadata.tables:
        legacy_metadata.remove(legacy_metadata.tables["employer_contacts"])
    # Notifications were introduced in 0013; they must not be bootstrapped by
    # the legacy snapshot copied from the current ORM metadata.
    if "notifications" in legacy_metadata.tables:
        legacy_metadata.remove(legacy_metadata.tables["notifications"])
    legacy_metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
