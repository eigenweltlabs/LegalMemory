"""thread message-id linkage for deterministic email threading

Revision ID: 7c3e9f5a2d14
Revises: 4a7d21c9b8e5
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c3e9f5a2d14"
down_revision: Union[str, Sequence[str], None] = "4a7d21c9b8e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "threads",
        sa.Column("message_ids", JSON, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("threads", "message_ids")
