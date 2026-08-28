"""engagement status: the work axis on matters, plus deterministic document-date bounds

Adds the SALI-LMSS-aligned engagement_status (open | waiting | closed | canceled)
with its close date, and the deterministic first/last document-date bounds. Also
merges the three open migration heads into one.

Revision ID: b8e3f1a2c9d4
Revises: 9b1f4c7e6a02, a71c4d9e2b60, cc9ef5e3d206
Create Date: 2026-08-28

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e3f1a2c9d4"
down_revision: Union[str, Sequence[str], None] = (
    "9b1f4c7e6a02",
    "a71c4d9e2b60",
    "cc9ef5e3d206",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "matters", sa.Column("engagement_status", sa.String(length=12), nullable=True)
    )
    op.add_column(
        "matters",
        sa.Column("engagement_close_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "matters",
        sa.Column("first_document_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "matters",
        sa.Column("last_document_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_matters_engagement_status", "matters", ["engagement_status"]
    )


def downgrade() -> None:
    op.drop_index("ix_matters_engagement_status", table_name="matters")
    op.drop_column("matters", "last_document_date")
    op.drop_column("matters", "first_document_date")
    op.drop_column("matters", "engagement_close_date")
    op.drop_column("matters", "engagement_status")
