"""connector event subscriptions

Revision ID: 8ef14a62d997
Revises: 40b5e5343e88
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8ef14a62d997"
down_revision: Union[str, Sequence[str], None] = "40b5e5343e88"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "connector_event_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("adapter", sa.String(length=80), nullable=False),
        sa.Column("transport", sa.String(length=40), nullable=False),
        sa.Column("target", sa.String(length=1024), nullable=False),
        sa.Column("external_id", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("detail", JSON, nullable=False),
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
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "adapter",
            "target",
            name="uq_connector_event_subscription_target",
        ),
    )
    op.create_index(
        "ix_connector_event_subscriptions_source_id",
        "connector_event_subscriptions",
        ["source_id"],
    )
    op.create_index(
        "ix_connector_event_subscriptions_external",
        "connector_event_subscriptions",
        ["adapter", "external_id"],
    )
    op.create_table(
        "connector_event_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("transport", sa.String(length=80), nullable=False),
        sa.Column("partition", sa.String(length=120), nullable=False),
        sa.Column("position", sa.String(length=255), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=True),
        sa.Column("detail", JSON, nullable=False),
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
        sa.UniqueConstraint(
            "transport",
            "partition",
            name="uq_connector_event_checkpoint_partition",
        ),
    )


def downgrade() -> None:
    op.drop_table("connector_event_checkpoints")
    op.drop_index(
        "ix_connector_event_subscriptions_external",
        table_name="connector_event_subscriptions",
    )
    op.drop_index(
        "ix_connector_event_subscriptions_source_id",
        table_name="connector_event_subscriptions",
    )
    op.drop_table("connector_event_subscriptions")
