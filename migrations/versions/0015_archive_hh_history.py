"""Remove HH sessions while retaining good vacancy history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The FK currently prevents retaining a vacancy after its session is gone.
    # Recreate the SQLite table with a nullable session_id first.
    with op.batch_alter_table("vacancies", recreate="always") as batch_op:
        batch_op.alter_column("session_id", existing_type=sa.Integer(), nullable=True)

    bind = op.get_bind()
    bad = "('ERROR', 'UNKNOWN', 'UNKNOW')"

    # Remove all records owned by invalid vacancies before removing the parent.
    for table in (
        "vacancy_snapshots",
        "evaluations",
        "application_plans",
        "cover_letters",
        "applications",
        "employer_contacts",
    ):
        bind.execute(text(
            f"DELETE FROM {table} WHERE vacancy_id IN "
            f"(SELECT id FROM vacancies WHERE state IN {bad})"
        ))
    bind.execute(text(f"DELETE FROM vacancies WHERE state IN {bad}"))

    # Keep HireHi's complete history, but detach good HH vacancies from the
    # sessions that are being removed.
    bind.execute(text(
        "UPDATE vacancies SET session_id = NULL "
        "WHERE source = 'hh' AND session_id IN "
        "(SELECT id FROM sessions WHERE adapter_id = 'hh')"
    ))
    bind.execute(text(
        "DELETE FROM browser_events WHERE session_id IN "
        "(SELECT id FROM sessions WHERE adapter_id = 'hh')"
    ))
    bind.execute(text(
        "DELETE FROM notifications WHERE source_type = 'session' AND source_id IN "
        "(SELECT CAST(id AS TEXT) FROM sessions WHERE adapter_id = 'hh')"
    ))
    bind.execute(text("DELETE FROM sessions WHERE adapter_id = 'hh'"))


def downgrade() -> None:
    raise RuntimeError("0015 archive cleanup is irreversible")
