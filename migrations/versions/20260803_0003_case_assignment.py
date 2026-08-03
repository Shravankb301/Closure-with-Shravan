"""persist case officer assignment

Revision ID: 20260803_0003
Revises: 20260802_0002
Create Date: 2026-08-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0003"
down_revision: str | None = "20260802_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("assigned_officer", sa.String(length=160)))
    op.add_column("cases", sa.Column("assigned_badge", sa.String(length=80)))
    op.add_column("cases", sa.Column("assigned_unit", sa.String(length=160)))
    op.add_column("cases", sa.Column("handoff_note", sa.Text()))


def downgrade() -> None:
    op.drop_column("cases", "handoff_note")
    op.drop_column("cases", "assigned_unit")
    op.drop_column("cases", "assigned_badge")
    op.drop_column("cases", "assigned_officer")
