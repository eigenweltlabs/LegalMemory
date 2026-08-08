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

The backfill runs the Python rule itself over the existing rows rather than an
equivalent written in SQL. A SQL rendering is easy to write and wrong in exactly the
way this module warns about: measured against the rule on five ordinary names,
`lower(unaccent(...))` produced "whitfield crane llp", "nordwind energie gmbh" and
"muller verwaltungs ag" where the application produces "whitfield crane", "nordwind
energie" and "mueller verwaltungs" — five keys out of five that the resolver would
never compute again. Every one of those rows would be invisible to the search that
is supposed to find it, and the next document naming the company would create the
twin this change exists to prevent.

The unique index is created on the deduplicated key. If existing data already holds
duplicates the migration stops and names them rather than picking a winner: choosing
which of two entities survives is a merge, and a migration must never perform one.
Repair the duplicates first (the estate this was measured on was repaired by a
separate maintenance pass), then migrate.

Revision ID: d9a41f6c73b2
Revises: c4e18b9d2f07
Create Date: 2026-08-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from knowledge_index.entity_names import normalize_entity_name

# revision identifiers, used by Alembic.
revision: str = "d9a41f6c73b2"
down_revision: Union[str, Sequence[str], None] = "c4e18b9d2f07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("clients", "parties")

# Rows per round trip. Large enough that a full estate is a handful of statements,
# small enough that the parameter list stays inside what the driver will bind.
_BACKFILL_BATCH = 500


def _backfill(connection, table: str) -> None:
    """Give every existing row the key the application would compute for it."""
    rows = connection.execute(sa.text(f"SELECT id, name FROM {table} ORDER BY id")).all()
    updates = [
        {"row_id": row_id, "normalized": normalize_entity_name(name)} for row_id, name in rows
    ]
    statement = sa.text(f"UPDATE {table} SET normalized_name = :normalized WHERE id = :row_id")
    for start in range(0, len(updates), _BACKFILL_BATCH):
        connection.execute(statement, updates[start : start + _BACKFILL_BATCH])


def _refuse_existing_duplicates(connection, table: str) -> None:
    """Stop before the index build, naming what has to be merged first.

    Postgres would refuse the index on its own, but it names one colliding value and
    calls it "could not create unique index" — which reads as a broken migration
    rather than as an estate holding two rows for one company. The names are the
    actionable part, so they are what this reports.
    """
    duplicates = connection.execute(
        sa.text(
            f"""
            SELECT normalized_name, count(*) AS rows
              FROM {table}
             GROUP BY normalized_name, identity_discriminator
            HAVING count(*) > 1
             ORDER BY rows DESC, normalized_name
             LIMIT 20
            """
        )
    ).all()
    if not duplicates:
        return
    total = connection.execute(
        sa.text(
            f"""
            SELECT count(*) FROM (
                SELECT 1 FROM {table}
                 GROUP BY normalized_name, identity_discriminator
                HAVING count(*) > 1
            ) AS collisions
            """
        )
    ).scalar()
    listed = ", ".join(f"{name!r} ({rows} rows)" for name, rows in duplicates)
    raise RuntimeError(
        f"{table} already holds {total} name(s) with more than one row, so the identity "
        f"constraint cannot be created: {listed}"
        + (" …" if total > len(duplicates) else "")
        + ". Merge them first — deciding which row survives is a data repair and a "
        "migration must never make that choice for you."
    )


def upgrade() -> None:
    # similarity() and the trigram GIN operator class the resolver's name search
    # rides on. IF NOT EXISTS so a deployment that already has it is untouched.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    connection = op.get_bind()
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
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
        )
        _backfill(connection, table)
        op.alter_column(table, "normalized_name", nullable=False)
        _refuse_existing_duplicates(connection, table)
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
