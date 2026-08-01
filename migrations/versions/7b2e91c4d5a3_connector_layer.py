"""Connector layer: encrypted credentials, mirrored memberships, staged content

Connectors now run in-process, which moves three things into this database:

* the OAuth credentials for a firm's document estate (encrypted; `sources.config` is
  documented as non-secret and is surfaced in the admin UI, so they cannot live there);
* the group memberships mirrored from a source, without which a grant to a source group
  matches no caller and the documents are invisible rather than protected;
* where a connector staged an object's bytes, so the fetch stage opens a local file
  instead of asking the connector to locate the object again — which for an API source
  means re-crawling the whole estate.

Revision ID: 7b2e91c4d5a3
Revises: 0cc0ca18a7f5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7b2e91c4d5a3"
down_revision: Union[str, Sequence[str], None] = "0cc0ca18a7f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_credentials",
        sa.Column("source_id", sa.String(length=36), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=60), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Dropping a source must not leave its refresh token behind.
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "source_group_members",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=255), nullable=False),
        sa.Column("member_id", sa.String(length=255), nullable=False),
        sa.Column("member_type", sa.String(length=10), nullable=False),
        sa.Column("group_name", sa.Text(), nullable=True),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", "group_id", "member_id", name="uq_source_group_member"),
    )
    # Every permission check expands a caller against this table, so the member lookup
    # has to be an index hit rather than a scan.
    op.create_index(
        "ix_source_group_members_member", "source_group_members", ["source_id", "member_id"]
    )

    op.add_column("source_objects", sa.Column("staged_path", sa.Text(), nullable=True))
    op.add_column(
        "sources", sa.Column("last_full_sync_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Which folder selection a source was last synced under, so narrowing the scope is
    # recognised as a deliberate re-scope rather than a suspicious mass deletion.
    op.add_column("sources", sa.Column("selection_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "selection_fingerprint")
    op.drop_column("sources", "last_full_sync_at")
    op.drop_column("source_objects", "staged_path")
    op.drop_index("ix_source_group_members_member", table_name="source_group_members")
    op.drop_table("source_group_members")
    op.drop_table("source_credentials")
