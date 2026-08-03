"""index matters.reference_numbers so matter lookup stops scanning the table

get_or_create_matter resolved a reference by loading every Matter row into
Python and scanning it in a loop — while holding the matter-ref advisory lock,
itself nested inside the matter-create lock. Fine at 60 matters; quadratic
across the ~1,300 a firm-scale corpus creates, and it stalled matter creation
to ~2 per five minutes on the 2026-08-03 51k run (93 tasks parked on advisory
locks, oldest wait 16 minutes).

reference_numbers is jsonb, so a containment lookup answers the same question
from an index. jsonb_path_ops is the smaller, faster GIN variant and supports
the @> operator this query uses.

Revision ID: c4e18b9d2f07
Revises: b7d3a5c91e04
Create Date: 2026-08-03

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e18b9d2f07'
down_revision: Union[str, Sequence[str], None] = 'b7d3a5c91e04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_matters_reference_numbers
        ON matters USING gin (reference_numbers jsonb_path_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_matters_reference_numbers")
