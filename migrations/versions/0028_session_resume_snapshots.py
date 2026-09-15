"""Add session-owned resume snapshots and one-use preview state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    from backend.persistence.models import ResumePreviewToken, SessionResumeSnapshot

    columns = {column["name"]: column for column in sa.inspect(bind).get_columns("sessions")}
    if "profile_id" in columns and not columns["profile_id"]["nullable"]:
        with op.batch_alter_table("sessions") as batch:
            batch.alter_column("profile_id", existing_type=sa.Integer(), nullable=True)
    if "session_answers" not in columns:
        with op.batch_alter_table("sessions") as batch:
            batch.add_column(sa.Column("session_answers", sa.JSON(), nullable=False, server_default="{}"))
    app_columns = {column["name"]: column for column in sa.inspect(bind).get_columns("applications")}
    if "candidate_profile_id" in app_columns and not app_columns["candidate_profile_id"]["nullable"]:
        with op.batch_alter_table("applications") as batch:
            batch.alter_column("candidate_profile_id", existing_type=sa.Integer(), nullable=True)
    SessionResumeSnapshot.__table__.create(bind, checkfirst=True)
    ResumePreviewToken.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    from backend.persistence.models import ResumePreviewToken, SessionResumeSnapshot

    ResumePreviewToken.__table__.drop(bind, checkfirst=True)
    SessionResumeSnapshot.__table__.drop(bind, checkfirst=True)
    # Do not make profile_id mandatory again: rows created by the new flow
    # cannot be losslessly downgraded to a profile.
