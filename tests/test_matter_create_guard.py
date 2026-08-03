"""Enforced matter-create protocol (2026-08-01 run audit): create_matter must
directly follow search_matters, and at the decision moment it replays the
agent's own queries under the create lock — so a sibling's just-created matter
is guaranteed to be seen, whatever reference string each model chose (the
"splinter matter" race).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import Matter, Source
from knowledge_index.pipeline.matter_search import classification_tools


@pytest.fixture
def tools_factory(factory: sessionmaker[Session]):
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.commit()
        source_id = source.id

    # Handlers read through their bound session; roll those sessions back at
    # teardown so no idle-in-transaction connection blocks the next test's
    # TRUNCATE reset.
    sessions: list[Session] = []

    def build(seen: set[str] | None = None):
        session = factory()
        sessions.append(session)
        tools = classification_tools(
            session,
            AppConfig(),
            source_id,
            "Mandate/M-2026-0036 Statement of Work/file.docx",
            session_factory=factory,
            project_id=None,
            fallback_reference="UNASSIGNED-X",
            provenance={"model": "test"},
            seen_matter_ids=seen if seen is not None else set(),
        )
        return {tool.name: tool for tool in tools}

    yield build
    for session in sessions:
        session.rollback()
        session.close()


def _matter_count(factory: sessionmaker) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(Matter))


def test_create_without_preceding_search_is_rejected(tools_factory, factory) -> None:
    tools = tools_factory()
    cold = json.loads(tools["create_matter"].handler({"reference_number": "M-1", "title": "T"}))
    assert cold["error"] == "stale_search"
    assert _matter_count(factory) == 0

    # search, then wander off — the create is stale again
    tools["search_matters"].handler({"query": "M-1"})
    tools["list_folder"].handler({})
    stale = json.loads(tools["create_matter"].handler({"reference_number": "M-1", "title": "T"}))
    assert stale["error"] == "stale_search"
    assert _matter_count(factory) == 0

    # search immediately before create: accepted
    tools["search_matters"].handler({"query": "M-1"})
    created = json.loads(tools["create_matter"].handler({"reference_number": "M-1", "title": "T"}))
    assert created["created"] is True
    assert _matter_count(factory) == 1


def test_race_with_divergent_references_is_surfaced(tools_factory, factory) -> None:
    # Doc A searches while the world is empty (the stale snapshot of the race).
    seen_a: set[str] = set()
    tools_a = tools_factory(seen_a)
    tools_a["search_matters"].handler({"query": "M-2026-0036"})
    assert seen_a == set()

    # Sibling doc B creates the real matter in the meantime.
    tools_b = tools_factory()
    tools_b["search_matters"].handler({"query": "M-2026-0036"})
    sibling = json.loads(
        tools_b["create_matter"].handler(
            {"reference_number": "M-2026-0036", "title": "Statement of Work"}
        )
    )
    assert sibling["created"] is True

    # Doc A now creates under a DIFFERENT reference (the document-internal
    # billing number), still acting on its pre-sibling search — adjacency is
    # satisfied, but the snapshot is stale: the replay must surface the
    # sibling instead of creating.
    refused = json.loads(
        tools_a["create_matter"].handler(
            {"reference_number": "10342.00019", "title": "Statement of Work — Helios/Prismara"}
        )
    )
    assert refused["error"] == "matter_list_changed"
    assert sibling["id"] in {row["id"] for row in refused["new_matters"]}
    # the sibling is now anchored for doc A: submitting its id passes the
    # seen-id validator, so the agent can simply assign to it
    assert sibling["id"] in seen_a
    assert _matter_count(factory) == 1

    # If the agent insists (a genuinely new matter), the confirming second
    # call goes through.
    confirmed = json.loads(
        tools_a["create_matter"].handler(
            {"reference_number": "10342.00019", "title": "Statement of Work — Helios/Prismara"}
        )
    )
    assert confirmed["created"] is True
    assert _matter_count(factory) == 2
