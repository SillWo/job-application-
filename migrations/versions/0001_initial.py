import sqlalchemy as sa
from alembic import op

from backend.persistence import models  # noqa: F401
from backend.persistence.database import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Pin the initial schema even when newer ORM models add columns. Without
    # these exclusions a clean `upgrade head` would create columns added by
    # later revisions here and following migrations would attempt to add them
    # again.
    legacy_metadata = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(legacy_metadata)
    reports = legacy_metadata.tables["reports"]
    reports._columns.remove(reports.c.pdf_path)
    sessions = legacy_metadata.tables["sessions"]
    sessions._columns.remove(sessions.c.viewed_limit)
    sessions._columns.remove(sessions.c.application_limit)
    legacy_metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
