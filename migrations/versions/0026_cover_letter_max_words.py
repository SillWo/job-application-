"""Store the optional per-session cover-letter word limit."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
    if "cover_letter_max_words" in columns:
        return
    with op.batch_alter_table("sessions") as batch:
        # NULL deliberately means "use the application default" so existing
        # sessions preserve their historical 150-word behavior.
        batch.add_column(sa.Column("cover_letter_max_words", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("cover_letter_max_words")
