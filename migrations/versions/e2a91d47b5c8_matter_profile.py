"""matter profile: lifecycle, profile payload, and the staleness queue

Revision ID: e2a91d47b5c8
Revises: d9a41f6c73b2
Create Date: 2026-08-09

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a91d47b5c8"
down_revision: Union[str, Sequence[str], None] = "d9a41f6c73b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("matters", sa.Column("lifecycle", sa.String(length=20), nullable=True))
    op.add_column("matters", sa.Column("profile", sa.JSON(), nullable=True))
    op.create_table(
        "matter_profile_queue",
        sa.Column("matter_id", sa.String(length=36), nullable=False),
        sa.Column("last_marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id"),
    )
    op.create_index(
        "ix_matter_profile_queue_marked", "matter_profile_queue", ["last_marked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_matter_profile_queue_marked", table_name="matter_profile_queue")
    op.drop_table("matter_profile_queue")
    op.drop_column("matters", "profile")
    op.drop_column("matters", "lifecycle")
