"""Durable bounded vacancy pipeline and broker-operation links."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``init_database`` uses metadata.create_all for a brand-new local
    # install.  If that compatibility path ran before Alembic, the complete
    # 0042 shape already exists and must be stamped without destructive
    # recreation.  Historical Alembic databases take the normal path below.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    required = {
        "pipeline_items",
        "pipeline_checkpoints",
        "pipeline_model_operations",
    }
    present = required & existing
    if present and present != required:
        raise RuntimeError(
            "Partial 0042 pipeline schema detected; refusing a mixed coordination schema"
        )
    if required <= existing:
        inspector = sa.inspect(op.get_bind())
        expected_columns = {
            "pipeline_items": {
                "id", "session_id", "site_id", "external_id", "vacancy_id", "stage",
                "status", "generation", "stage_revision", "request_id", "diagnostic_id",
                "error_code", "created_at", "updated_at",
            },
            "pipeline_checkpoints": {
                "id", "session_id", "site_id", "name", "generation", "revision", "data",
                "updated_at",
            },
            "pipeline_model_operations": {
                "id", "pipeline_item_id", "session_id", "site_id", "vacancy_key", "stage",
                "role", "input_hash", "versions_hash", "generation", "request_id",
                "diagnostic_id", "status", "created_at", "updated_at",
            },
        }
        expected_indexes = {
            "pipeline_items": {
                "ix_pipeline_items_site_queue",
                "ix_pipeline_items_session_queue",
                "ix_pipeline_items_vacancy",
            },
            "pipeline_checkpoints": {"ix_pipeline_checkpoints_session"},
            "pipeline_model_operations": {
                "ix_pipeline_model_ops_request",
                "ix_pipeline_model_ops_session",
            },
        }
        expected_uniques = {
            "pipeline_items": {"uq_pipeline_item_identity"},
            "pipeline_checkpoints": {"uq_pipeline_checkpoint_identity"},
            "pipeline_model_operations": {"uq_pipeline_model_operation"},
        }
        expected_checks = {
            "pipeline_items": {
                "ck_pipeline_items_stage",
                "ck_pipeline_items_status",
                "ck_pipeline_items_generation_nonnegative",
            },
            "pipeline_checkpoints": {"ck_pipeline_checkpoints_generation_nonnegative"},
            "pipeline_model_operations": {
                "ck_pipeline_model_ops_generation_nonnegative",
                "ck_pipeline_model_ops_status",
            },
        }
        for table in required:
            columns = {column["name"] for column in inspector.get_columns(table)}
            indexes = {index["name"] for index in inspector.get_indexes(table)}
            uniques = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table)
                if constraint.get("name")
            }
            checks = {
                constraint["name"]
                for constraint in inspector.get_check_constraints(table)
                if constraint.get("name")
            }
            if (
                columns != expected_columns[table]
                or not expected_indexes[table] <= indexes
                or not expected_uniques[table] <= uniques
                or not expected_checks[table] <= checks
            ):
                raise RuntimeError(
                    f"Existing {table} does not match the complete 0042 coordination schema"
                )
        return
    op.create_table(
        "pipeline_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), sa.ForeignKey("vacancies.id", ondelete="SET NULL")),
        sa.Column("stage", sa.String(32), nullable=False, server_default="discovery"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(36)),
        sa.Column("diagnostic_id", sa.String(36)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('discovery','extraction','evaluation','letter','submission','reporting','completed')",
            name="ck_pipeline_items_stage",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_pipeline_items_status",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_pipeline_items_generation_nonnegative"),
        sa.UniqueConstraint("session_id", "site_id", "external_id", "generation", name="uq_pipeline_item_identity"),
    )
    op.create_index("ix_pipeline_items_site_queue", "pipeline_items", ["site_id", "stage", "status", "created_at", "id"])
    op.create_index("ix_pipeline_items_session_queue", "pipeline_items", ["session_id", "generation", "status", "stage"])
    op.create_index("ix_pipeline_items_vacancy", "pipeline_items", ["vacancy_id"])

    op.create_table(
        "pipeline_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", sa.String(50), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_pipeline_checkpoints_generation_nonnegative"),
        sa.UniqueConstraint("session_id", "site_id", "name", "generation", name="uq_pipeline_checkpoint_identity"),
    )
    op.create_index("ix_pipeline_checkpoints_session", "pipeline_checkpoints", ["session_id", "generation", "name"])

    op.create_table(
        "pipeline_model_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_item_id", sa.Integer(), sa.ForeignKey("pipeline_items.id", ondelete="CASCADE")),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_id", sa.String(50), nullable=False),
        sa.Column("vacancy_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("stage", sa.String(100), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("versions_hash", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(36)),
        sa.Column("diagnostic_id", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitting"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_pipeline_model_ops_generation_nonnegative"),
        sa.CheckConstraint(
            "status IN ('submitting','queued','running','retry','completed','failed','cancelled')",
            name="ck_pipeline_model_ops_status",
        ),
        sa.UniqueConstraint(
            "session_id", "site_id", "vacancy_key", "stage", "role", "input_hash",
            "versions_hash", "generation", name="uq_pipeline_model_operation",
        ),
    )
    op.create_index("ix_pipeline_model_ops_request", "pipeline_model_operations", ["request_id"])
    op.create_index("ix_pipeline_model_ops_session", "pipeline_model_operations", ["session_id", "generation", "stage"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_model_ops_session", table_name="pipeline_model_operations")
    op.drop_index("ix_pipeline_model_ops_request", table_name="pipeline_model_operations")
    op.drop_table("pipeline_model_operations")
    op.drop_index("ix_pipeline_checkpoints_session", table_name="pipeline_checkpoints")
    op.drop_table("pipeline_checkpoints")
    op.drop_index("ix_pipeline_items_vacancy", table_name="pipeline_items")
    op.drop_index("ix_pipeline_items_session_queue", table_name="pipeline_items")
    op.drop_index("ix_pipeline_items_site_queue", table_name="pipeline_items")
    op.drop_table("pipeline_items")
