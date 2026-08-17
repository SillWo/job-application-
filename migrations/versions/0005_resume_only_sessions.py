"""Make policies legacy-only and persist the resume score threshold.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns("sessions")
    }
    if "score_threshold" not in columns:
        op.add_column(
            "sessions",
            sa.Column(
                "score_threshold",
                sa.Integer(),
                nullable=False,
                server_default="75",
            ),
        )
    if not columns["policy_id"]["nullable"]:
        with op.batch_alter_table("sessions") as batch_op:
            batch_op.alter_column(
                "policy_id",
                existing_type=sa.Integer(),
                nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    sessions = sa.table(
        "sessions",
        sa.column("policy_id", sa.Integer()),
    )
    missing_policy = bind.execute(
        sa.select(sa.func.count()).select_from(sessions).where(
            sessions.c.policy_id.is_(None)
        )
    ).scalar_one()
    if missing_policy:
        policies = sa.table(
            "search_policies",
            sa.column("id", sa.Integer()),
            sa.column("suitable_text", sa.Text()),
            sa.column("excluded_text", sa.Text()),
            sa.column("filters", sa.JSON()),
            sa.column("compiled", sa.JSON()),
            sa.column("confirmed", sa.Boolean()),
        )
        bind.execute(
            sa.insert(policies).values(
                suitable_text="Legacy resume-only session",
                excluded_text="",
                filters={},
                compiled={},
                confirmed=False,
            )
        )
        legacy_policy_id = bind.execute(
            sa.select(sa.func.max(policies.c.id))
        ).scalar_one()
        bind.execute(
            sa.update(sessions)
            .where(sessions.c.policy_id.is_(None))
            .values(policy_id=legacy_policy_id)
        )
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "policy_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    op.drop_column("sessions", "score_threshold")
