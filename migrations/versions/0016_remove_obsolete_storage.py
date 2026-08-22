"""Remove storage no longer used by the current product."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("employer_contacts"):
        op.drop_table("employer_contacts")
    if inspector.has_table("site_accounts"):
        op.drop_table("site_accounts")
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    with op.batch_alter_table("sessions", recreate="always") as batch_op:
        for column in ("policy_id", "resume_url", "resume_path"):
            if column in session_columns:
                batch_op.drop_column(column)
    if inspector.has_table("search_policies"):
        op.drop_table("search_policies")
    profile_columns = {column["name"] for column in inspector.get_columns("candidate_profiles")}
    with op.batch_alter_table("candidate_profiles", recreate="always") as batch_op:
        for column in ("filename", "data", "resume_path"):
            if column in profile_columns:
                batch_op.drop_column(column)


def downgrade() -> None:
    raise RuntimeError("0016 obsolete storage removal is irreversible")
