"""Alembic environment for the Knowledge Index schema.

Uses the SQLAlchemy models as the single source of truth and the same
``KI_DATABASE_URL`` the application uses, so migrations and the app never diverge.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, text

from knowledge_index.db.models import Base

config = context.config
target_metadata = Base.metadata

DATABASE_URL = os.environ.get(
    "KI_DATABASE_URL", "postgresql+pg8000://ki:ki-dev-only@localhost:5439/ki"
)


def _render_item(type_, obj, autogen_context):
    """Render pgvector's Vector type with its import so migrations are self-contained."""
    import pgvector.sqlalchemy

    if type_ == "type" and isinstance(obj, pgvector.sqlalchemy.Vector):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.Vector(dim={obj.dim})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=_render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=_render_item,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
