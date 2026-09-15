"""Repair nullable candidate profile references in legacy applications."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow applications created without a candidate profile.

    Some installations were stamped at 0033 while retaining the pre-0028
    NOT NULL constraint.  SQLite needs a batch table rebuild for this change;
    Alembic preserves the existing rows, foreign keys, and unique indexes.
    The inspection makes this repair safe for already-correct or partial DBs.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("applications"):
        return
    columns = {column["name"]: column for column in inspector.get_columns("applications")}
    candidate_profile_id = columns.get("candidate_profile_id")
    if candidate_profile_id is not None and not candidate_profile_id["nullable"]:
        with op.batch_alter_table("applications") as batch:
            batch.alter_column(
                "candidate_profile_id",
                existing_type=sa.Integer(),
                nullable=True,
            )


def downgrade() -> None:
    # The repair is intentionally irreversible: existing rows may have NULL
    # candidate_profile_id values after 0034.
    pass
