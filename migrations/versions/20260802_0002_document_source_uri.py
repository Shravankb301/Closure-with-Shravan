"""retain document source URIs

Revision ID: 20260802_0002
Revises: 20260731_0001
Create Date: 2026-08-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0002"
down_revision: str | None = "20260731_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_uri", sa.String(length=2048), nullable=True))
    op.add_column(
        "assertions",
        sa.Column("time_precision", sa.String(length=16), server_default="EXACT", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("assertions", "time_precision")
    op.drop_column("documents", "source_uri")
