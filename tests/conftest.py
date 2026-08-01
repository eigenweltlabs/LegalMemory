"""Shared fixtures: every test runs against real services — no mocks, no fallbacks.

All tests use a dedicated Postgres database (``ki_test`` on localhost:5439).
Integration tests additionally require the live LiteLLM gateway, Docling Serve,
and OpenSearch; the ``live_stack`` fixture skips loudly when any of them is down.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig, ComponentsConfig, RetrievalConfig
from knowledge_index.db.models import Base, ProcessingState
from knowledge_index.pipeline import PipelineRunner

POSTGRES_ADMIN_URL = "postgresql+pg8000://ki:ki-dev-only@localhost:5439/postgres"
# Two concurrent runs against one database deadlock on the per-test TRUNCATE — which
# surfaces as unrelated tests "failing", not as a lock error, so it costs a real
# investigation every time. Set KI_TEST_DATABASE to give a run its own database.
TEST_DATABASE = os.environ.get("KI_TEST_DATABASE", "ki_test")
TEST_DATABASE_URL = f"postgresql+pg8000://ki:ki-dev-only@localhost:5439/{TEST_DATABASE}"
# Advisory-lock key claimed for the whole run; see the guard in pg_factory. Arbitrary
# but fixed, and scoped to the database it is taken in, so a run in its own
# KI_TEST_DATABASE never blocks one in ki_test.
_RUN_LOCK = 0x4B495F54455354


# The suite runs against whatever models the deployment names, exactly as the appliance
# does. Defaulted here so a checkout with no .env still runs, and read through the same
# two variables so a test can never assert a model name the product does not use.
os.environ.setdefault("KI_LLM_MODEL", "qwen3.6-35b-a3b")
os.environ.setdefault("KI_EMBEDDING_MODEL", "text-embedding-3-small")
TEST_LLM_MODEL = os.environ["KI_LLM_MODEL"]
TEST_EMBEDDING_MODEL = os.environ["KI_EMBEDDING_MODEL"]

LITELLM_URL = "http://localhost:4000"
DOCLING_URL = "http://localhost:5001"
OPENSEARCH_URL = "http://localhost:9200"


@pytest.fixture(scope="session")
def pg_factory():
    """Ensure the ``ki_test`` database and schema exist; yield its sessionmaker.

    Reused rather than dropped and recreated on every run. Dropping it mid-session
    invalidates any pooled connection another engine already opened, which surfaced as
    "database ki_test does not exist" or "relation ... does not exist" failures that moved
    around between runs. Per-test isolation comes from the TRUNCATE in ``factory``.

    It is rebuilt only when the models no longer match what is on disk — ``create_all``
    adds missing tables but never missing *columns*, so a schema change would otherwise
    fail every test with a confusing "column does not exist". That check happens before
    any other engine exists, so the destructive path cannot pull the rug out from under a
    live connection.
    """
    try:
        _ensure_database(recreate=False)
        if _schema_is_stale():
            _ensure_database(recreate=True)
    # pytest.exit raises a BaseException subclass, so it passes through this handler.
    except Exception as exc:
        pytest.exit(
            "Postgres is not reachable on localhost:5439 — run `docker compose up -d postgres` "
            f"first; tests run against real services only. ({type(exc).__name__}: {exc})",
            returncode=4,
        )
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(engine)

    # One run per database, enforced rather than documented. The comment on
    # TEST_DATABASE has warned about concurrent runs for a while; without a check the
    # second run still starts, and the two then corrupt each other's fixtures — the
    # TRUNCATE in `factory` races the other run's inserts, `restore_engine` drops a
    # database the other run is restoring into, and the damage surfaces as a scatter of
    # unrelated failures across backup, sync and MCP tests. Observed cost of not having
    # this: 29 red tests in a 14-minute run, none of which was broken.
    #
    # A session-level advisory lock is the cheapest honest guard: it is held by this
    # connection for exactly as long as the run lasts, and it is released automatically
    # if the process dies, so a killed run never leaves the database locked.
    guard = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    if not guard.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _RUN_LOCK}).scalar():
        guard.close()
        engine.dispose()
        pytest.exit(
            f"Another pytest run is already using {TEST_DATABASE}. Two runs against one "
            "database corrupt each other's fixtures and fail in ways that look like real "
            "bugs. Wait for it to finish, or give this run its own database with "
            "KI_TEST_DATABASE=<other-name>.",
            returncode=4,
        )

    yield sessionmaker(engine, expire_on_commit=False)
    guard.close()
    engine.dispose()


def _ensure_database(*, recreate: bool) -> None:
    admin_engine = create_engine(POSTGRES_ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            if recreate:
                connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DATABASE},
            ).scalar()
            if not exists:
                connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    finally:
        admin_engine.dispose()
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.commit()
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


def _schema_is_stale() -> bool:
    """Whether any model column or index is missing from the existing test database.

    Indexes count: some of them carry behaviour rather than performance (the partial
    unique index that allows only one unfinished sync run per source, for instance), and
    a test database silently missing one would pass a test that production would fail.
    """
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            present: dict[str, set[str]] = {}
            for table_name, column_name in connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            ):
                present.setdefault(table_name, set()).add(column_name)
            indexes = {
                row[0]
                for row in connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                )
            }
        for table in Base.metadata.sorted_tables:
            existing = present.get(table.name)
            if existing is None:
                continue  # create_all will add a wholly new table
            if {column.name for column in table.columns} - existing:
                return True
            if {index.name for index in table.indexes} - indexes:
                return True
        return False
    finally:
        engine.dispose()


@pytest.fixture
def factory(pg_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """Per-test isolation without schema churn: truncate every table up front."""
    tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    with pg_factory() as session:
        # Blocking here means another run holds the same database. Without a timeout that
        # waits indefinitely and reads as a hung or failing test suite; fail fast and name
        # the actual cause instead.
        session.execute(text("SET lock_timeout = '15s'"))
        try:
            session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        except OperationalError as exc:  # pragma: no cover - contention only
            raise RuntimeError(
                f"Could not reset {TEST_DATABASE}: another test run is holding it. "
                "Set KI_TEST_DATABASE=<other-name> to run concurrently."
            ) from exc
        session.commit()
    return pg_factory


@pytest.fixture
def session(factory: sessionmaker[Session]):
    """One ORM session on the truncated test database; commit and close on teardown."""
    session = factory()
    yield session
    try:
        session.commit()
    finally:
        session.close()


@pytest.fixture(scope="session")
def live_stack() -> None:
    """Integration tests only: verify LiteLLM, Docling Serve, and OpenSearch are live.

    Never fakes a response — a missing service skips with the exact command to start it.
    """
    # Match docker-compose.yml's local-development default so an otherwise
    # unconfigured fresh stack can exercise authenticated model endpoints.
    os.environ.setdefault("LITELLM_MASTER_KEY", "sk-lm-dev-only")
    master_key = os.environ["LITELLM_MASTER_KEY"]

    try:
        response = httpx.get(f"{LITELLM_URL}/health/liveliness", timeout=5)
        if response.status_code == 404:
            response = httpx.get(
                f"{LITELLM_URL}/health",
                headers={"authorization": f"Bearer {master_key}"},
                timeout=15,
            )
        response.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"LiteLLM gateway is not reachable at {LITELLM_URL} "
            f"({type(exc).__name__}: {exc}) — integration tests call real models only. "
            "Start it with `docker compose up -d litellm` and export LITELLM_MASTER_KEY."
        )

    try:
        response = httpx.get(f"{DOCLING_URL}/health", timeout=5)
        if response.status_code == 404:
            response = httpx.get(f"{DOCLING_URL}/docs", timeout=5)
        response.raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"Docling Serve is not reachable at {DOCLING_URL} "
            f"({type(exc).__name__}: {exc}) — integration tests convert documents for real. "
            "Start it with `docker compose up -d docling`."
        )

    try:
        httpx.get(f"{OPENSEARCH_URL}/_cluster/health", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"OpenSearch is not reachable at {OPENSEARCH_URL} "
            f"({type(exc).__name__}: {exc}) — integration tests index chunks for real. "
            "Start it with `docker compose up -d opensearch`."
        )


@pytest.fixture
def integration_config(live_stack: None, tmp_path):
    """Real-stack AppConfig: localhost service URLs, per-test artifact dir and index.

    Model assignments stay at their deployment defaults (KI_LLM_MODEL for every
    stage, KI_EMBEDDING_MODEL for retrieval.embedding_model).
    """
    config = AppConfig(
        artifact_dir=tmp_path / "artifacts",
        components=ComponentsConfig(
            litellm_url=LITELLM_URL,
            docling_url=DOCLING_URL,
            opensearch_url=OPENSEARCH_URL,
        ),
        retrieval=RetrievalConfig(
            index_name=f"knowledge-index-test-{uuid.uuid4().hex[:12]}",
            embedding_dimensions=1536,
        ),
    )
    yield config
    try:
        httpx.delete(f"{OPENSEARCH_URL}/{config.retrieval.index_name}", timeout=10)
    except httpx.HTTPError:
        pass


@pytest.fixture
def settle_pipeline():
    """Run the real pipeline (including its retry semantics) until every stage settles."""

    def _settle(
        factory: sessionmaker[Session], config: AppConfig, *, timeout_seconds: int = 900
    ) -> dict[str, int]:
        deadline = time.monotonic() + timeout_seconds
        totals = {"processed": 0, "quarantined": 0}
        while True:
            result = PipelineRunner(factory, config).run_until_idle()
            totals["processed"] += result.processed
            totals["quarantined"] += result.quarantined
            with factory() as session:
                open_states = [
                    (state.stage, state.status, state.last_error)
                    for state in session.scalars(
                        select(ProcessingState).where(
                            ProcessingState.status.in_(["pending", "running", "failed"])
                        )
                    ).all()
                ]
            if not open_states:
                return totals
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"pipeline did not settle within {timeout_seconds}s; "
                    f"open stages: {open_states}"
                )
            time.sleep(2)

    return _settle


@pytest.fixture
def refresh_search():
    """Force an OpenSearch refresh so freshly indexed chunks are searchable."""

    def _refresh(config: AppConfig) -> None:
        base = config.components.opensearch_url.rstrip("/")
        response = httpx.post(f"{base}/{config.retrieval.index_name}/_refresh", timeout=10)
        if response.status_code != 404:
            response.raise_for_status()

    return _refresh
