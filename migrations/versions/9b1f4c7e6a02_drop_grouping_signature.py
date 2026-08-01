"""drop the dead matter-grouping cache key

The matter-wide grouping flow it cached for was removed (the file-scoped relate
flow replaced it); the column was written by nothing and read by nothing.

Revision ID: 9b1f4c7e6a02
Revises: 7c3e9f5a2d14
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b1f4c7e6a02"
down_revision: Union[str, Sequence[str], None] = "7c3e9f5a2d14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("matters", "grouping_signature")


def downgrade() -> None:
    op.add_column("matters", sa.Column("grouping_signature", sa.Text(), nullable=True))
