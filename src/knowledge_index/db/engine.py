"""Engine/session helpers. DATABASE_URL points at Postgres (pgvector required)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.db.models import Base

DEFAULT_URL = "postgresql+pg8000://ki:ki-dev-only@localhost:5439/ki"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            os.environ.get("KI_DATABASE_URL", DEFAULT_URL),
            pool_pre_ping=True,
            pool_size=int(os.environ.get("KI_DATABASE_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("KI_DATABASE_MAX_OVERFLOW", "10")),
            pool_timeout=float(os.environ.get("KI_DATABASE_POOL_TIMEOUT_SECONDS", "30")),
        )
    return _engine


@contextmanager
def get_session() -> Iterator[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    # Alembic migrations are the schema source of truth. Fall back to create_all only
    # when the migration files are not on disk (e.g. a minimal test harness).
    if not _upgrade_to_head():
        Base.metadata.create_all(engine)


def _upgrade_to_head() -> bool:
    """Run ``alembic upgrade head`` if the migrations are reachable; return success."""
    from pathlib import Path

    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for base in candidates:
        ini = base / "alembic.ini"
        if ini.exists() and (base / "migrations").is_dir():
            try:
                from alembic import command
                from alembic.config import Config

                config = Config(str(ini))
                config.set_main_option("script_location", str(base / "migrations"))
                command.upgrade(config, "head")
                return True
            except Exception:
                return False
    return False
