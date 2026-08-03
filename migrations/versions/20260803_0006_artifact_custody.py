"""add artifact custody storage and chained acquisition attempts

Revision ID: 20260803_0006
Revises: 20260803_0005
Create Date: 2026-08-03 22:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0006"
down_revision: str | None = "20260803_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_acquisitions",
        sa.Column(
            "acquisition_method",
            sa.String(length=40),
            server_default="PUBLIC_HTTP",
            nullable=False,
        ),
    )
    op.add_column(
        "source_acquisitions",
        sa.Column(
            "storage_status",
            sa.String(length=30),
            server_default="NOT_STORED",
            nullable=False,
        ),
    )
    op.add_column(
        "source_acquisitions",
        sa.Column("storage_uri", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "source_acquisitions",
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_table(
        "source_acquisition_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("acquisition_method", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_uri", sa.String(length=2048), nullable=True),
        sa.Column("previous_event_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "sequence",
            name="uq_source_acquisition_attempt_sequence",
        ),
        sa.UniqueConstraint("event_hash", name="uq_source_acquisition_attempt_hash"),
    )
    op.create_index(
        op.f("ix_source_acquisition_attempts_case_id"),
        "source_acquisition_attempts",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_acquisition_attempts_document_id"),
        "source_acquisition_attempts",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_source_acquisition_attempts_document_id"),
        table_name="source_acquisition_attempts",
    )
    op.drop_index(
        op.f("ix_source_acquisition_attempts_case_id"),
        table_name="source_acquisition_attempts",
    )
    op.drop_table("source_acquisition_attempts")
    op.drop_column("source_acquisitions", "attempt_count")
    op.drop_column("source_acquisitions", "storage_uri")
    op.drop_column("source_acquisitions", "storage_status")
    op.drop_column("source_acquisitions", "acquisition_method")
