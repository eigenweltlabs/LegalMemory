"""ontology-typed documents: ancestor closure + scope fingerprint columns

doc_type now holds an ontology node id (knowledge_index.ontology) instead of a
taxonomy enum value; existing enum values are left in place and simply never
match a subtree filter until their documents are re-typed by the metadata stage.

Revision ID: 7b4e2d1a9c30
Revises: 0cc0ca18a7f5
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7b4e2d1a9c30'
down_revision: Union[str, Sequence[str], None] = '23307d6ab3a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("doc_type_ancestors", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False, server_default="[]"),
    )
    op.add_column("documents", sa.Column("ontology_fingerprint", sa.String(length=16), nullable=True))
    op.add_column(
        "chunks",
        sa.Column("doc_type_ancestors", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("chunks", "doc_type_ancestors")
    op.drop_column("documents", "ontology_fingerprint")
    op.drop_column("documents", "doc_type_ancestors")
