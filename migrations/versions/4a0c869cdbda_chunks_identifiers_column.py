"""chunks.identifiers column

Persist the per-chunk identifiers list. It is denormalized from
Document.identifiers (like doc_type/language/doc_date on the same table) and
feeds the OpenSearch identifiers / identifiers_text fields. Before this it was
assigned in the index stage as a transient Python attribute with no backing
column, so it reached OpenSearch only via getattr on in-memory objects and was
never stored — a resync built from DB rows silently dropped it.

Revision ID: 4a0c869cdbda
Revises: 7b4e2d1a9c30
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4a0c869cdbda'
down_revision: Union[str, Sequence[str], None] = '9c1d4e7f2a58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add NOT NULL with a temporary server_default so existing rows backfill to
    # []; then drop the default so runtime inserts come from the model (default=list),
    # matching allowed_principals/denied_principals on this table.
    op.add_column(
        'chunks',
        sa.Column(
            'identifiers',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.alter_column('chunks', 'identifiers', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chunks', 'identifiers')
