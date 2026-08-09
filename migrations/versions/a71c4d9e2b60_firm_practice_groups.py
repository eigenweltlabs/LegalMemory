"""practice groups as entities rather than free strings

A group written four ways is four books of business, and a caller asking what
the capital markets group has done gets a quarter of it. Same treatment the firm
people already get: one row per group, keyed on a normalized name, with every
other spelling recorded as an alias.

Revision ID: a71c4d9e2b60
Revises: f3b82c095ad1
Create Date: 2026-08-09

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a71c4d9e2b60"
down_revision: Union[str, Sequence[str], None] = "f3b82c095ad1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "firm_practice_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_name", name="uq_firm_practice_groups_normalized_name"
        ),
    )


def downgrade() -> None:
    op.drop_table("firm_practice_groups")
