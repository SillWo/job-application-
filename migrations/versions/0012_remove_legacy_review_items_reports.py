from alembic import op
from sqlalchemy import inspect

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    # Older deployments may retain these tables despite migration 0008.
    # Check first so fresh upgrades and already-clean databases both work.
    for table in ("review_items", "reports"):
        if table in inspector.get_table_names():
            op.drop_table(table)


def downgrade() -> None:
    # Legacy tables were removed by 0008 and have no supported schema to restore.
    pass
