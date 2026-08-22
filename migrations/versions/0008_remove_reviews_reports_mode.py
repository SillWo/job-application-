"""Remove obsolete review/report persistence and session mode."""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("reports")
    op.drop_table("review_items")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("mode")


def downgrade() -> None:
    raise NotImplementedError("Removed reports/reviews cannot be restored automatically")
