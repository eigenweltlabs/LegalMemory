"""widen matters.matter_kind for ontology node ids (Service facet)

Revision ID: 9c1d4e7f2a58
Revises: 7b4e2d1a9c30
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9c1d4e7f2a58'
down_revision: Union[str, Sequence[str], None] = '7b4e2d1a9c30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "matters",
        "matter_kind",
        existing_type=sa.String(length=20),
        type_=sa.String(length=50),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "matters",
        "matter_kind",
        existing_type=sa.String(length=50),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
