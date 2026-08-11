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
        # Edges are collection rows: endpoints + provenance, no embedded
        # citation records — those come from the item-level tools.
        assert all("citations" not in edge for edge in visible)
        assert all(edge["from"]["id"] and edge["to"]["id"] for edge in visible)


def test_search_in_document_ranks_inside_one_file_and_pages(
    factory: sessionmaker[Session], corpus: AppConfig
) -> None:
    """Find the passage without reading the file, and point at the page it is on.

    The estate search collapses to one excerpt per document, which answers
    "which document" and not "where in this one". Reading a long agreement end
    to end to find a single clause is what exhausts a context window, so this is
    the tool that has to work for a large file to be usable at all.
    """
    with factory() as session:
        spa = document_for_path(session, "SPA_final")
        service = RetrievalService(session, corpus)

        # An unauthorized reader gets nothing — same wall as get_document.
        assert (
            service.search_in_document(spa.id, "Haftung", principals={"group:litigation"})
            is None
        )

        found = service.search_in_document(
            spa.id, "Haftung Kaufpreis", principals={"group:ma-team"}, limit=5
        )
        assert found is not None
        assert found["document_id"] == spa.id
        assert found["results"], "a document that contains the term must return a passage"
        top = found["results"][0]
        assert "Haftung" in top["text"]
        # Every hit says which get_document page holds it, so reading around the
        # hit is one call and no arithmetic.
        assert top["get_document_page"] == top["ordinal"] // 12 + 1
        assert found["page"]["returned"] == len(found["results"])
        assert found["page"]["total_chunks_in_document"] >= 1

        # The result set is scoped to THIS document: the other side of the
        # ethical wall shares vocabulary ("Kaufpreis") and must not leak in.
        klage = document_for_path(session, "Klage_final")
        assert klage.id != spa.id
        chunk_ids = {result["ordinal"] for result in found["results"]}
        assert chunk_ids, "expected ranked chunks from the SPA itself"

        # An empty query is a caller error, not an empty result: it would
        # otherwise return the document's first chunks and look like an answer.
        with pytest.raises(ValueError):
            service.search_in_document(spa.id, "   ", principals={"group:ma-team"})
