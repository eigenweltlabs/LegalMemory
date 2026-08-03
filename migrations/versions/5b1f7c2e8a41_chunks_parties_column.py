"""chunks.parties column

Persist the per-chunk parties list. It is denormalized from Document.parties
(like identifiers/doc_type/language on the same table) and feeds the OpenSearch
`parties` keyword field that backs the F4 party filter. Each entry is a keyword
term — a party's resolved party_id or its canonical name — so a caller can filter
by either. A real column (not a transient attribute) so a resync built from DB
rows carries it, matching the identifiers column.

Revision ID: 5b1f7c2e8a41
Revises: 4a0c869cdbda
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5b1f7c2e8a41'
down_revision: Union[str, Sequence[str], None] = '4a0c869cdbda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOT NULL with a temporary server_default so existing rows backfill to [];
    # then drop the default so runtime inserts come from the model (default=list),
    # matching identifiers/allowed_principals on this table.
    op.add_column(
        'chunks',
        sa.Column(
            'parties',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.alter_column('chunks', 'parties', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chunks', 'parties')
