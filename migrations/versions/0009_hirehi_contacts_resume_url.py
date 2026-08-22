"""Persist HireHi employer contacts and session resume URL."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("resume_url", sa.String(length=1000), nullable=True))
    op.create_table(
        "employer_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id"), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("vacancy_id", "kind", "value"),
    )
    op.create_index("ix_employer_contacts_vacancy_id", "employer_contacts", ["vacancy_id"])


def downgrade() -> None:
    op.drop_index("ix_employer_contacts_vacancy_id", table_name="employer_contacts")
    op.drop_table("employer_contacts")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("resume_url")
