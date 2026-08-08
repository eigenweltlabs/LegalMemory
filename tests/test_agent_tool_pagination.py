"""The ingestion agents' own tools page too, and say when they truncated.

These tools are not on the MCP surface — they are what the classification,
relation and party-resolution agents call while a document is being indexed —
but they had the same defect and it costs more here: an agent that reads "no
matching matter" from a page that was merely full creates a duplicate matter,
and an agent that reads the first 12,000 characters of a contract as the whole
contract mislabels it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Blob,
    Matter,
    Party,
    Source,
    SourceObject,
)
from knowledge_index.pipeline.matter_search import (
    classification_tools,
    open_source_file,
    party_resolution_tools,
    search_matters_page,
)


@pytest.fixture
def source_id(factory: sessionmaker[Session]) -> str:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.commit()
        return source.id


def _seed_matters(session: Session, count: int, *, prefix: str = "Fischer") -> None:
    for index in range(count):
        session.add(
            Matter(
                id=f"m-{index:03d}",
                reference_numbers=[f"M-2026-{index:04d}"],
                title=f"{prefix} Handelsstreit {index:03d}",
            )
        )
    session.commit()


def test_search_matters_pages_a_crowded_result(factory) -> None:
    with factory() as session:
        _seed_matters(session, 20)

    with factory() as session:
        first = search_matters_page(
            session, AppConfig(), "Fischer", limit=5, include_semantic=False
        )
        assert len(first.items) == 5
        assert first.total == 20
        assert first.has_more is True

        second = search_matters_page(
            session, AppConfig(), "Fischer", limit=5, offset=5, include_semantic=False
        )
        assert {item["id"] for item in first.items} & {
            item["id"] for item in second.items
        } == set()


def test_a_reference_number_is_findable_past_the_old_scan_bound(factory) -> None:
    """The reference leg used to be a Python scan over an unordered
    ``select(Matter).limit(2000)``: past 2,000 matters, whether a given
    Aktenzeichen was findable depended on physical row order."""
    with factory() as session:
        _seed_matters(session, 2100)

    with factory() as session:
        page = search_matters_page(
            session, AppConfig(), "M-2026-2050", limit=5, include_semantic=False
        )
        assert [item["id"] for item in page.items][:1] == ["m-2050"]


def test_the_search_matters_tool_returns_a_page_envelope(factory, source_id) -> None:
    with factory() as session:
        _seed_matters(session, 12)

    session = factory()
    try:
        tools = {
            tool.name: tool
            for tool in classification_tools(
                session, AppConfig(), source_id, "Mandate/x/file.docx"
            )
        }
        payload = json.loads(
            tools["search_matters"].handler({"query": "Fischer", "limit": 4})
        )
        assert set(payload) == {"results", "page"}
        assert len(payload["results"]) == 4
        assert payload["page"]["has_more"] is True
        assert payload["page"]["total"] == 12

        rest = json.loads(
            tools["search_matters"].handler(
                {"query": "Fischer", "limit": 20, "offset": payload["page"]["next_offset"]}
            )
        )
        assert len(rest["results"]) == 8
        assert rest["page"]["has_more"] is False
    finally:
        session.rollback()
        session.close()


def test_seen_matter_ids_track_every_page(factory, source_id) -> None:
    """The create guard rejects a matter_id the agent never saw, so paging has to
    keep feeding that set — otherwise page 2's matters are unassignable."""
    with factory() as session:
        _seed_matters(session, 12)

    session = factory()
    try:
        seen: set[str] = set()
        tools = {
            tool.name: tool
            for tool in classification_tools(
                session,
                AppConfig(),
                source_id,
                "Mandate/x/file.docx",
                seen_matter_ids=seen,
            )
        }
        tools["search_matters"].handler({"query": "Fischer", "limit": 4})
        first_page = set(seen)
        tools["search_matters"].handler({"query": "Fischer", "limit": 4, "offset": 4})
        assert len(seen) == 8
        assert first_page < seen
    finally:
        session.rollback()
        session.close()


def test_search_entities_tool_pages(factory) -> None:
    with factory() as session:
        for index in range(9):
            session.add(
                Party(
                    id=f"party-{index}",
                    name=f"Nordwind Energie {index} GmbH",
                    kind="company",
                )
            )
        session.commit()

    session = factory()
    try:
        seen: set[str] = set()
        tool = party_resolution_tools(session, AppConfig(), seen)[0]
        payload = json.loads(tool.handler({"query": "Nordwind", "limit": 4}))
        assert len(payload["results"]) == 4
        assert payload["page"]["has_more"] is True
        assert payload["page"]["total"] == 9
        # Only what the agent was actually shown counts as seen.
        assert len(seen) == 4
    finally:
        session.rollback()
        session.close()


def test_open_file_says_when_it_returned_a_prefix(factory, source_id) -> None:
    body = "x" * 30_000
    with factory() as session:
        blob = Blob(content_hash="hash-long", size_bytes=len(body))
        session.add(blob)
        session.flush()
        session.add_all(
            [
                SourceObject(
                    id="so-long",
                    source_id=source_id,
                    external_id="external/long",
                    path="M-1/long.docx",
                    name="long.docx",
                    container="M-1",
                    content_hash="hash-long",
                ),
                Artifact(
                    content_hash="hash-long",
                    producer="test",
                    producer_version="1",
                    kind="structured_json",
                    payload={"text": body},
                ),
            ]
        )
        session.commit()

    with factory() as session:
        first = open_source_file(session, source_id, "M-1/long.docx", max_chars=12_000)
        assert first["returned_chars"] == 12_000
        assert first["total_chars"] == 30_000
        # The point of the change: continuation is stated, not left as arithmetic.
        assert first["has_more"] is True
        assert first["next_offset"] == 12_000

        last = open_source_file(
            session, source_id, "M-1/long.docx", offset=24_000, max_chars=12_000
        )
        assert last["returned_chars"] == 6_000
        assert last["has_more"] is False
        assert last["next_offset"] is None


def test_peek_matter_flags_a_sampled_title_list(factory, source_id) -> None:
    from knowledge_index.db.models import Document

    with factory() as session:
        session.add(Matter(id="m-big", reference_numbers=["M-1"], title="Big matter"))
        session.flush()
        for index in range(20):
            session.add(
                Document(
                    id=f"doc-{index:03d}",
                    matter_id="m-big",
                    title=f"Schriftsatz {index:03d}",
                )
            )
        session.commit()

    from knowledge_index.pipeline.matter_search import peek_matter

    with factory() as session:
        summary = peek_matter(session, "m-big")
        assert len(summary["document_titles"]) == 12
        assert summary["document_count"] == 20
        assert summary["document_titles_are_sample"] is True

    with factory() as session:
        session.add(Matter(id="m-small", reference_numbers=["M-2"], title="Small"))
        session.commit()

    with factory() as session:
        summary = peek_matter(session, "m-small")
        assert summary["document_titles"] == []
        assert summary["document_count"] == 0
        assert summary["document_titles_are_sample"] is False
