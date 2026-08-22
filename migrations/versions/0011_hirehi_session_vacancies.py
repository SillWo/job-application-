from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s_%(column_2_name)s"
}


def upgrade() -> None:
    with op.batch_alter_table(
        "vacancies",
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_vacancies_source_external_id_",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_vacancies_session_source_external_id",
            ["session_id", "source", "external_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "vacancies",
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_vacancies_session_source_external_id",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_vacancies_source_external_id",
            ["source", "external_id"],
        )
