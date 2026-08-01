"""relation intents — parked relate decisions replayed after classify

Revision ID: 4a7d21c9b8e5
Revises: 8ef14a62d997
Create Date: 2026-07-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4a7d21c9b8e5"
down_revision: Union[str, Sequence[str], None] = "8ef14a62d997"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "relation_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_object_id", sa.String(length=36), nullable=False),
        sa.Column("target_source_object_id", sa.String(length=36), nullable=False),
        sa.Column("intent", sa.String(length=20), nullable=False),
        sa.Column("relation_kind", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("provenance", JSON, nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["source_object_id"], ["source_objects.id"]),
        sa.ForeignKeyConstraint(["target_source_object_id"], ["source_objects.id"]),
        sa.UniqueConstraint(
            "source_object_id",
            "target_source_object_id",
            "intent",
            "relation_kind",
            name="uq_relation_intent",
        ),
    )
    op.create_index(
        "ix_relation_intents_target",
        "relation_intents",
        ["target_source_object_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_relation_intents_target", table_name="relation_intents")
    op.drop_table("relation_intents")
