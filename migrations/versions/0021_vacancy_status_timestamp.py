"""Track vacancy status timestamps and display platforms."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("vacancies")}
    if "status_changed_at" not in columns or "site" not in columns:
        with op.batch_alter_table("vacancies") as batch:
            if "status_changed_at" not in columns:
                batch.add_column(
                    sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True)
                )
            if "site" not in columns:
                batch.add_column(sa.Column("site", sa.String(100), nullable=True))
    op.execute(
        "UPDATE vacancies SET status_changed_at = '2026-09-01 00:00:00' "
        "WHERE status_changed_at IS NULL"
    )
    op.execute("UPDATE vacancies SET site = '' WHERE site IS NULL")
    refreshed = {
        column["name"]: column for column in sa.inspect(bind).get_columns("vacancies")
    }
    nullable_columns = {
        name for name in ("status_changed_at", "site") if refreshed[name]["nullable"]
    }
    if nullable_columns:
        with op.batch_alter_table("vacancies") as batch:
            for name in nullable_columns:
                batch.alter_column(name, nullable=False)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("vacancies")}
    if "ix_vacancies_status_changed_at" not in indexes:
        op.create_index("ix_vacancies_status_changed_at", "vacancies", ["status_changed_at"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("vacancies")}
    if "ix_vacancies_status_changed_at" in indexes:
        op.drop_index("ix_vacancies_status_changed_at", table_name="vacancies")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("vacancies")}
    with op.batch_alter_table("vacancies") as batch:
        if "status_changed_at" in columns:
            batch.drop_column("status_changed_at")
        if "site" in columns:
            batch.drop_column("site")
