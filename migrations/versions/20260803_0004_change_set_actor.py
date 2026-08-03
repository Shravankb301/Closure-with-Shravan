"""snapshot the reviewer responsible for evidence mutations

Revision ID: 20260803_0004
Revises: 20260803_0003
Create Date: 2026-08-03 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0004"
down_revision: str | None = "20260803_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "change_sets",
        sa.Column("performed_by", sa.String(length=160), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("change_sets", "performed_by")
