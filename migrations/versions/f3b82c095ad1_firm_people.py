"""firm people and matter teams

A matter's practice group is the group of the partner who owns it, and the firm
writes that down: "Priya Anand, Responsible Partner, Litigation Department". This
gives that fact a home — people as entities, roles as links — instead of a string
copied onto the matter.

Revision ID: f3b82c095ad1
Revises: e2a91d47b5c8
Create Date: 2026-08-09

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b82c095ad1"
down_revision: Union[str, Sequence[str], None] = "e2a91d47b5c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "firm_people",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("practice_group", sa.String(length=120), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name", name="uq_firm_people_normalized_name"),
    )
    op.create_index("ix_firm_people_practice_group", "firm_people", ["practice_group"])
    op.create_table(
        "matter_team",
        sa.Column("matter_id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=60), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["matter_id"], ["matters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["firm_people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id", "person_id", "role"),
    )
    op.create_index("ix_matter_team_person", "matter_team", ["person_id", "role"])
    op.add_column(
        "matters", sa.Column("practice_group", sa.String(length=120), nullable=True)
    )
    op.create_index("ix_matters_practice_group", "matters", ["practice_group"])


def downgrade() -> None:
    op.drop_index("ix_matters_practice_group", table_name="matters")
    op.drop_column("matters", "practice_group")
    op.drop_index("ix_matter_team_person", table_name="matter_team")
    op.drop_table("matter_team")
    op.drop_index("ix_firm_people_practice_group", table_name="firm_people")
    op.drop_table("firm_people")
