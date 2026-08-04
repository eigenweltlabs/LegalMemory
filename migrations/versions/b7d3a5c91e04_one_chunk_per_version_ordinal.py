"""one chunk per (version, ordinal) — dedupe then constrain

Two index executions racing on a merged version (each source file of the
merge carries its own index task) could store every chunk twice, and the
re-index diff could never see the duplicate again (2026-08-01 audit: 96
duplicate pairs across 4 versions). Delete the shadowed copies (keep the
oldest row per position), then make the database refuse new twins.
OpenSearch copies of the deleted rows are healed by the duplicate sweep
in the index stage on that version's next re-index.

Revision ID: b7d3a5c91e04
Revises: 5b5463a9548f
Create Date: 2026-08-01

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7d3a5c91e04'
down_revision: Union[str, Sequence[str], None] = '5b5463a9548f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM chunks a
        USING chunks b
        WHERE a.document_version_id = b.document_version_id
          AND a.ordinal = b.ordinal
          AND a.id > b.id
        """
    )
    # Idempotent: after the 2026-08-01 incident the constraint was hotfixed
    # straight onto the demo database, so a plain ADD CONSTRAINT collides (42P07,
    # "already exists") whenever Alembic's version pointer is still behind this
    # revision. Add it only when it isn't already there.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_chunks_version_ordinal'
            ) THEN
                ALTER TABLE chunks
                    ADD CONSTRAINT uq_chunks_version_ordinal
                    UNIQUE (document_version_id, ordinal);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_chunks_version_ordinal", "chunks", type_="unique")
