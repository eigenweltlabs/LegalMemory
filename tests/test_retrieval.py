"""Retrieval and ethical-wall tests against the live stack.

Documents are located by source path (not by model-assigned doc_type), and
assertions are structural: authorized principals get hits, walled principals
get nothing, and taxonomy fields stay within the controlled vocabulary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Relation,
    Source,
    SourceObject,
)
from knowledge_index.retrieval import RetrievalService, SearchFilters
from knowledge_index.sync import LocalFilesystemSource, SyncEngine
from knowledge_index.config import AppConfig as _AppConfig

pytestmark = pytest.mark.integration

ONTOLOGY_NODES = _AppConfig().doc_ontology().visible


@pytest.fixture
def corpus(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    refresh_search,
    tmp_path: Path,
) -> AppConfig:
    """Two documents behind different ethical walls, fully indexed by the real pipeline."""
    root = tmp_path / "source"
    root.mkdir()
    (root / "M-2026-0042_SPA_final.txt").write_text(
        "Finaler Unternehmenskaufvertrag. Haftung ist auf den Kaufpreis begrenzt.",
        encoding="utf-8",
    )
    (root / "M-2026-0099_Klage_final.txt").write_text(
        "Klageschrift wegen einer offenen Kaufpreisforderung.", encoding="utf-8"
    )
    acl_by_path = {
        "M-2026-0042_SPA_final.txt": [
            {"principal": "group:ma-team", "principal_kind": "group", "access": "allow"}
        ],
        "M-2026-0099_Klage_final.txt": [
            {
                "principal": "group:litigation",
                "principal_kind": "group",
                "access": "allow",
            }
        ],
    }
    with factory() as session:
        source = Source(
            kind="local_fs",
            display_name="fixture",
            config={"root": str(root), "acl_by_path": acl_by_path},
        )
        session.add(source)
        session.flush()
        SyncEngine(
            session,
            source,
            LocalFilesystemSource(root, acl_resolver=lambda p: acl_by_path[p.name]),
        ).sync()
        session.commit()
    totals = settle_pipeline(factory, integration_config)
    assert totals["quarantined"] == 0
    refresh_search(integration_config)
    return integration_config


def document_for_path(session: Session, path_fragment: str) -> Document:
    document = session.scalars(
        select(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(
            DocumentVersionSource,
            DocumentVersionSource.version_id == DocumentVersion.id,
        )
        .join(SourceObject, SourceObject.id == DocumentVersionSource.source_object_id)
        .where(SourceObject.path.contains(path_fragment))
    ).first()
    assert document is not None, f"no document produced for source path *{path_fragment}*"
    return document


def test_search_enforces_ethical_walls_before_ranking(
    factory: sessionmaker[Session], corpus: AppConfig
) -> None:
    with factory() as session:
        service = RetrievalService(session, corpus)
        ma_hits = service.search_semantic("Kaufpreis Haftung", principals={"group:ma-team"})
        assert len(ma_hits) >= 1
        assert all(hit.doc_type is None or hit.doc_type in ONTOLOGY_NODES for hit in ma_hits)
        assert all("0099" not in path for hit in ma_hits for path in hit.source_paths)
        assert all(hit.citations for hit in ma_hits)
        assert all(hit.citations[0]["matched_chunk"]["id"] for hit in ma_hits)
        assert all(hit.citations[0]["source_objects"] for hit in ma_hits)

        litigation_hits = service.search_filter(
            principals={"group:litigation"}, filters=SearchFilters(version_status="final")
        )
        assert len(litigation_hits) >= 1
        assert all(hit.version_status == "final" for hit in litigation_hits)
        assert all(hit.citations for hit in litigation_hits)
        assert all(hit.citations[0]["document"]["project_id"] == hit.project_id for hit in litigation_hits)
        assert all(hit.citations[0]["source_objects"] for hit in litigation_hits)
        assert all(
            "0042" not in path for hit in litigation_hits for path in hit.source_paths
        )

        assert service.search_filter(principals={"user:outsider"}) == []


def test_get_document_never_returns_unauthorized_content(
    factory: sessionmaker[Session], corpus: AppConfig
) -> None:
    with factory() as session:
        spa = document_for_path(session, "SPA_final")
        service = RetrievalService(session, corpus)
        assert service.get_document(spa.id, principals={"group:litigation"}) is None
        visible = service.get_document(spa.id, principals={"group:ma-team"})
        assert visible is not None
        assert "Haftung" in visible["content"]["text"]
        assert visible["citations"][0]["document"]["id"] == spa.id
        assert visible["citations"][0]["source_objects"]


def test_traversal_resolves_document_edges_back_to_authorized_sources(
    factory: sessionmaker[Session], corpus: AppConfig
) -> None:
    with factory() as session:
        ma = document_for_path(session, "SPA_final")
        litigation = document_for_path(session, "Klage_final")
        assert ma.id != litigation.id
        existing = session.scalar(
            select(Relation).where(
                Relation.from_type == "document",
                Relation.from_id == ma.id,
                Relation.to_type == "document",
                Relation.to_id == litigation.id,
                Relation.kind == "references",
            )
        )
        if existing is None:
            session.add(
                Relation(
                    from_type="document",
                    from_id=ma.id,
                    to_type="document",
                    to_id=litigation.id,
                    kind="references",
                    provenance={"model": "test"},
                )
            )
            session.commit()

        service = RetrievalService(session, corpus)
        assert service.traverse("document", ma.id, principals={"user:outsider"}) == []
        # The caller can see the anchor but not the cross-wall target, so the edge is hidden.
        assert service.traverse("document", ma.id, principals={"group:ma-team"}) == []
        visible = service.traverse(
            "document", ma.id, principals={"group:ma-team", "group:litigation"}
        )
        assert any(
            edge["kind"] == "references" and edge["to"]["id"] == litigation.id
            for edge in visible
        )
        assert all(edge["citations"] for edge in visible)
        assert all(citation["source_objects"] for edge in visible for citation in edge["citations"])


def test_search_hit_carries_the_whole_chunk_not_a_window() -> None:
    """The tool payload is the chunk; the 320-char window is console decoration.

    Chunks are chosen by semantic similarity and ``_excerpt`` cut them by the position
    of the first literal query term, so the two criteria disagreed: a correctly
    retrieved chunk could be represented by an irrelevant third of itself, and the
    reranker then rated the document on that third.
    """
    from knowledge_index.retrieval import _MAX_HIT_TEXT_CHARS, SearchHit, _excerpt, _hit_text

    chunk = (
        "Silverpine has been represented throughout by Ashford & Cromdale Consulting LLP, "
        "with Catherine Aldridge serving as lead partner. "
        + ("filler sentence to push the answer well past any 320-character window. " * 8)
        + "Saxonbrook has been represented by Pinnacle Law Group LLP."
    )
    assert len(chunk) > 320  # the case the window used to lose

    hit = SearchHit(
        project_id=None, document_id="d", version_id="v", matter_id="m",
        title="Internal Negotiation Summary Memorandum", doc_type=None,
        version_status="final", score=0.5,
        text=_hit_text(chunk), excerpt=_excerpt(chunk, {"silverpine"}),
    )

    payload = hit.as_dict()
    assert payload["text"] == chunk  # whole chunk, verbatim
    assert "Pinnacle Law Group LLP" in payload["text"]  # the part a window dropped
    assert "excerpt" not in payload  # display-only, never the model's payload
    # The window still works, and still would have missed the answer.
    assert len(hit.excerpt) < len(chunk)
    assert "Pinnacle Law Group LLP" not in hit.excerpt


def test_hit_text_flags_a_pathological_chunk_instead_of_trimming_silently() -> None:
    """Splitting cannot always hit its target; a caller must be able to tell."""
    from knowledge_index.retrieval import _MAX_HIT_TEXT_CHARS, _hit_text

    ordinary = "x" * (_MAX_HIT_TEXT_CHARS - 1)
    assert _hit_text(ordinary) == ordinary  # untouched, as almost every chunk is

    oversized = "y" * (_MAX_HIT_TEXT_CHARS + 5000)
    out = _hit_text(oversized)
    assert len(out) < len(oversized)
    assert "chunk truncated" in out and "get_document" in out
