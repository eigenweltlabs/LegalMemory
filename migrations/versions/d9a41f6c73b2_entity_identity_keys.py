"""give clients and parties an identity key, and make a twin impossible

Insertion created a new entity nearly every time it met one: on a 9,288-document
run whose ground truth is 46 clients, the clients table held 1,212 rows, 1,076 of
them touching exactly one matter. 973 of those creations reused a normalized name
the estate already held, and 293 of THOSE happened within 60 seconds of the
previous one — a plain race between documents extracting in parallel, with nothing
in the schema able to refuse it.

This adds the key the resolver matches on and the constraint that makes the race
unwinnable:

* normalized_name — case-folded, transliterated, diacritic-folded,
  punctuation-stripped, legal-form-stripped (knowledge_index.entity_names). Written
  by the application rather than by a generated column, deliberately: the rule has
  to be byte-identical in the resolver and in the constraint, and a plpgsql copy of
  it is how the two drift apart.
* identity_discriminator — empty for every entity the estate has no reason to split.
  Two genuinely different companies do share a name; when a mention carries a
  register identifier that contradicts the same-named incumbent, the new row records
  it here and the unique index admits the second entity. Nothing else gets past.
* normalized_aliases — every other name form seen for the entity, so a variant the
  corpus has already taught the resolver matches exactly next time.

The backfill computes normalized_name in SQL for existing rows. It is an
APPROXIMATION of the Python rule (it has no legal-form list), which is fine for one
purpose only: giving every existing row a non-empty key so the unique index can be
built. Rows this repair leaves merged or split wrongly are the data repair's
problem, not the schema's — and a fresh estate never runs it.

The unique index is created on the deduplicated key. If existing data already holds
duplicates the index build fails loudly rather than picking a winner: choosing which
of two entities survives is a merge, and a migration must never perform one.

Revision ID: d9a41f6c73b2
Revises: c4e18b9d2f07
Create Date: 2026-08-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9a41f6c73b2"
down_revision: Union[str, Sequence[str], None] = "c4e18b9d2f07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("clients", "parties")


def upgrade() -> None:
    # similarity() and the trigram GIN operator class the resolver's name search
    # rides on. IF NOT EXISTS so a deployment that already has it is untouched.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    for table in TABLES:
        op.add_column(table, sa.Column("normalized_name", sa.Text(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "identity_discriminator", sa.Text(), nullable=False, server_default=""
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "normalized_aliases",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
        )
        # unaccent is not IMMUTABLE, so it cannot appear in an index expression —
        # here it only writes a column value, which is fine.
        op.execute(
            f"""
            UPDATE {table}
               SET normalized_name = trim(regexp_replace(
                     lower(unaccent(translate(name, '&', ' '))),
                     '[^a-z0-9]+', ' ', 'g'))
            """
        )
        op.alter_column(table, "normalized_name", nullable=False)
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS ix_{table}_normalized_name_trgm
            ON {table} USING gin (normalized_name gin_trgm_ops)
            """
        )
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS ix_{table}_normalized_aliases
            ON {table} USING gin (normalized_aliases jsonb_path_ops)
            """
        )
        op.create_unique_constraint(
            f"uq_{table}_identity", table, ["normalized_name", "identity_discriminator"]
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(f"uq_{table}_identity", table, type_="unique")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_normalized_aliases")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_normalized_name_trgm")
        op.drop_column(table, "normalized_aliases")
        op.drop_column(table, "identity_discriminator")
        op.drop_column(table, "normalized_name")
