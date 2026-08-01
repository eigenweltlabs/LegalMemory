"""Pending source authorizations: a connection is not a row until the provider says yes

An OAuth connection used to be written to `sources` the moment the operator submitted
the connect form, before the browser handshake had even started. Closing the tab at the
consent screen therefore left a connection that could never sync, showing "Awaiting
authorization", zero objects and "Never" synced, forever — and an orphaned
`source_credentials` row holding the firm's client secret behind it.

The operator's intent now lives here until the provider answers. The row carries exactly
what the deferred `Source` will be made of, plus the firm's client id, client secret and
PKCE verifier as AES-256-GCM ciphertext under the same key as `source_credentials` — a
client secret is no less sensitive for belonging to a connection that does not exist yet.
It is deleted the moment the callback resolves, either way, and expires on its own if
nobody comes back.

Revision ID: f5320edaf89a
Revises: 7b2e91c4d5a3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f5320edaf89a"
down_revision: Union[str, Sequence[str], None] = "7b2e91c4d5a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pending_source_authorizations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        # The only field in the clear: it is the random value the provider echoes back,
        # and the callback has to be able to match on it.
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("config", JSON_VARIANT, nullable=False),
        sa.Column("sync_policy", JSON_VARIANT, nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_connection_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),  # base64(nonce || ciphertext)
        sa.Column("key_fingerprint", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # A handshake cannot outlive the project it was meant to file its documents under.
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        # One handshake per state, enforced by the database rather than by the callback.
        sa.UniqueConstraint("state", name="uq_pending_source_auth_state"),
    )
    # The sweep deletes by deadline on every new handshake and every callback.
    op.create_index(
        "ix_pending_source_auth_expires", "pending_source_authorizations", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_pending_source_auth_expires", table_name="pending_source_authorizations")
    op.drop_table("pending_source_authorizations")
