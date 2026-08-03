"""record public source acquisition metadata

Revision ID: 20260803_0005
Revises: 20260803_0004
Create Date: 2026-08-03 20:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0005"
down_revision: str | None = "20260803_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_acquisitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("requested_uri", sa.String(length=2048), nullable=False),
        sa.Column("resolved_uri", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=160), nullable=True),
        sa.Column("content_bytes", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("extraction_method", sa.String(length=50), nullable=False),
        sa.Column("extracted_characters", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("assertions_total", sa.Integer(), nullable=False),
        sa.Column("assertions_verified", sa.Integer(), nullable=False),
        sa.Column("verification_status", sa.String(length=30), nullable=False),
        sa.Column("error_class", sa.String(length=120), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_source_acquisition_document"),
    )
    op.create_index(
        op.f("ix_source_acquisitions_case_id"),
        "source_acquisitions",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_acquisitions_document_id"),
        "source_acquisitions",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_source_acquisitions_document_id"), table_name="source_acquisitions")
    op.drop_index(op.f("ix_source_acquisitions_case_id"), table_name="source_acquisitions")
    op.drop_table("source_acquisitions")
