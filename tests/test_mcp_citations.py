"""Citation-contract tests for every evidence-bearing MCP retrieval path."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.artifacts import LocalArtifactStore
from knowledge_index.db.models import (
    Artifact,
    BillingInvoice,
    Blob,
    Client,
    DecisionRecord,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    MatterClient,
    Project,
    ProjectGrant,
    Relation,
    Source,
    SourceObject,
)
from knowledge_index.mcp_server import _invoice_citations
from knowledge_index.retrieval import RetrievalService
from knowledge_index.web.app import create_app


PRINCIPALS = {"group:citation-test"}


def _seed_cited_document(session: Session) -> dict[str, object]:
    project = Project(id="project-1", key="P-001", name="Citation project", status="active")
    matter = Matter(
        id="matter-1",
        project_id=project.id,
        reference_numbers=["M-001"],
        title="Citation matter",
    )
    source = Source(
        id="source-1",
        project_id=project.id,
        kind="local_fs",
        display_name="Primary DMS",
        provider="native",
        config={"root": "/srv/dms"},
    )
    source_object = SourceObject(
        id="source-object-1",
        source_id=source.id,
        external_id="external/doc-1",
        path="M-001/Agreement.docx",
        name="Agreement.docx",
        container="M-001",
        content_hash="hash-1",
        source_version_label="v7",
    )
    blob = Blob(content_hash="hash-1", size_bytes=42)
    document = Document(
        id="document-1",
        project_id=project.id,
        matter_id=matter.id,
        title="Agreement",
        doc_type="contract",
        language="en",
    )
    version = DocumentVersion(
        id="version-1",
        document_id=document.id,
        content_hash=blob.content_hash,
        ordinal=1,
        status="final",
    )
    document.latest_final_version_id = version.id
    client = Client(id="client-1", name="Citation GmbH", kind="company")
    session.add_all([project, source, blob, client])
    session.flush()
    session.add_all(
        [
            ProjectGrant(
                project_id=project.id,
                principal="group:citation-test",
                effect="allow",
                role="viewer",
            ),
            matter,
            source_object,
        ]
    )
    session.flush()
    session.add(document)
    session.flush()
    session.add(version)
    session.flush()
    session.add_all(
        [
            DocumentVersionSource(
                version_id=version.id,
                source_object_id=source_object.id,
            ),
            Artifact(
                content_hash=blob.content_hash,
                producer="test",
                producer_version="1",
                kind="structured_json",
                payload={"text": "The liability cap is EUR 1,000,000."},
            ),
            DecisionRecord(
                id="decision-1",
                matter_id=matter.id,
                document_id=document.id,
                version_to=version.id,
                locus="Liability",
                change_summary="Added a cap",
                rationale_category="risk_allocation",
                rationale_text="The cap limits exposure.",
                source_evidence=[{"source_object_id": source_object.id}],
            ),
            Relation(
                from_type="document",
                from_id=document.id,
                to_type="matter",
                to_id=matter.id,
                kind="belongs_to",
            ),
            MatterClient(matter_id=matter.id, client_id=client.id),
        ]
    )
    session.flush()
    invoice = BillingInvoice(
        id="invoice-1",
        invoice_number="INV-001",
        matter_id=matter.id,
        invoice_total=1000.0,
        currency="EUR",
        source_object_id=source_object.id,
    )
    session.add(invoice)
    session.commit()
    return {
        "project": project,
        "matter": matter,
        "source": source,
        "source_object": source_object,
        "document": document,
        "version": version,
        "client": client,
        "invoice": invoice,
    }


def _seed_downloadable_pair(session: Session, artifact_dir) -> tuple[bytes, str]:
    original = b"PK\x03\x04exact-original-docx-bytes"
    related_original = b"PK\x03\x04related-original-docx-bytes"
    stored = LocalArtifactStore(artifact_dir).put_blob(
        BytesIO(original), max_bytes=1024 * 1024
    )
    related_stored = LocalArtifactStore(artifact_dir).put_blob(
        BytesIO(related_original), max_bytes=1024 * 1024
    )
    project = Project(
        id="download-project",
        key="DL-001",
        name="Download project",
        status="active",
    )
    matter = Matter(
        id="download-matter",
        project_id=project.id,
        reference_numbers=["DL-001"],
        title="Download matter",
    )
    source = Source(
        id="download-source",
        project_id=project.id,
        kind="local_fs",
        display_name="Download DMS",
        provider="native",
        config={"root": "/srv/dms"},
    )
    root_source = SourceObject(
        id="download-source-object",
        source_id=source.id,
        external_id="DL-001/original.docx",
        path="DL-001/original.docx",
        name="original.docx",
        container="DL-001",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(original),
        content_hash=stored.content_hash,
    )
    related_source = SourceObject(
        id="related-source-object",
        source_id=source.id,
        external_id="DL-001/related.docx",
        path="DL-001/related.docx",
        name="related.docx",
        container="DL-001",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(related_original),
        content_hash=related_stored.content_hash,
    )
    root_blob = Blob(
        content_hash=stored.content_hash,
        size_bytes=len(original),
        mime_sniffed=root_source.mime_type,
        cached_path=str(stored.path),
    )
    related_blob = Blob(
        content_hash=related_stored.content_hash,
        size_bytes=len(related_original),
        mime_sniffed=related_source.mime_type,
        cached_path=str(related_stored.path),
    )
    root_document = Document(
        id="download-document",
        project_id=project.id,
        matter_id=matter.id,
        title="Original agreement",
        doc_type="contract",
    )
    root_version = DocumentVersion(
        id="download-version",
        document_id=root_document.id,
        content_hash=stored.content_hash,
        ordinal=1,
        status="final",
    )
    root_document.latest_final_version_id = root_version.id
    related_document = Document(
        id="related-document",
        project_id=project.id,
        matter_id=matter.id,
        title="Related agreement",
        doc_type="contract",
    )
    related_version = DocumentVersion(
        id="related-version",
        document_id=related_document.id,
        content_hash=related_stored.content_hash,
        ordinal=1,
        status="final",
    )
    related_document.latest_final_version_id = related_version.id
    session.add_all([project, root_blob, related_blob, source])
    session.flush()
    session.add_all(
        [
            ProjectGrant(
                project_id=project.id,
                principal="group:citation-test",
                effect="allow",
                role="viewer",
            ),
            matter,
            root_source,
            related_source,
        ]
    )
    session.flush()
    session.add_all([root_document, related_document])
    session.flush()
    session.add_all([root_version, related_version])
    session.flush()
    session.add_all(
        [
            DocumentVersionSource(
                version_id=root_version.id,
                source_object_id=root_source.id,
            ),
            DocumentVersionSource(
                version_id=related_version.id,
                source_object_id=related_source.id,
            ),
            Artifact(
                content_hash=stored.content_hash,
                producer="test",
                producer_version="1",
                kind="structured_json",
                payload={
                    "text": "0123456789abcdefghijklmnopqrstuvwxyz",
                    "metadata": {"large": "x" * 10_000},
                },
            ),
            Artifact(
                content_hash=related_stored.content_hash,
                producer="test",
                producer_version="1",
                kind="structured_json",
                payload={"text": "Related content"},
            ),
            Relation(
                from_type="document",
                from_id=root_document.id,
                to_type="document",
                to_id=related_document.id,
                kind="references",
                provenance={"method": "test"},
            ),
        ]
    )
    session.commit()
    return original, stored.content_hash


def _assert_exact_citation(citation: dict) -> None:
    assert citation["project"] == {
        "id": "project-1",
        "key": "P-001",
        "name": "Citation project",
    }
    assert citation["document"]["id"] == "document-1"
    assert citation["document"]["project_id"] == "project-1"
    assert citation["version"]["id"] == "version-1"
    assert citation["version"]["content_hash"] == "hash-1"
    assert citation["source_objects"] == [
        {
            "id": "source-object-1",
            "source_id": "source-1",
            "external_id": "external/doc-1",
            "path": "M-001/Agreement.docx",
            "name": "Agreement.docx",
            "container": "M-001",
            "source_version_label": "v7",
            "connector": {
                "id": "source-1",
                "project_id": "project-1",
                "kind": "local_fs",
                "display_name": "Primary DMS",
                "provider": "native",
            },
        }
    ]


def _header_auth_config(artifact_dir) -> AppConfig:
    """Config for the tests that drive MCP tools by header instead of by OAuth.

    Header identity is the development escape hatch and is off unless asked for; the
    secure default and the real token flow live in test_mcp_oauth.py.
    """

    config = AppConfig(artifact_dir=artifact_dir)
    config.security.mcp_allow_trusted_header = True
    return config


def _call_mcp(client: TestClient, name: str, arguments: dict | None = None):
    response = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "x-ki-principals": "group:citation-test",
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    return structured.get("result", structured)


def test_all_document_query_paths_return_exact_citations(
    session: Session, tmp_path
) -> None:
    seeded = _seed_cited_document(session)
    service = RetrievalService(session, AppConfig(artifact_dir=tmp_path / "artifacts"))

    hit = service._hit_from_source(
        {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "text": "The liability cap is EUR 1,000,000.",
            "meta": {
                "source_object_id": "source-object-1",
                "kind": "chunk",
                "locus": "Liability",
            },
        },
        principals=PRINCIPALS,
        query_terms={"liability"},
        score=1.0,
        chunk_id="chunk-1",
    )
    assert hit is not None
    _assert_exact_citation(hit.citations[0])
    assert hit.citations[0]["matched_chunk"] == {
        "id": "chunk-1",
        "source_object_id": "source-object-1",
        "kind": "chunk",
        "locus": "Liability",
    }

    fetched = service.get_document("document-1", principals=PRINCIPALS)
    assert fetched is not None
    _assert_exact_citation(fetched["citations"][0])
    assert fetched["document"]["project_id"] == "project-1"

    matters = service.list_matters(principals=PRINCIPALS)
    assert matters[0]["project_id"] == "project-1"
    _assert_exact_citation(matters[0]["citations"][0])

    decisions = service.search_decisions("limits exposure", principals=PRINCIPALS)
    assert decisions[0]["project_id"] == "project-1"
    _assert_exact_citation(decisions[0]["citations"][0])

    edges = service.traverse("document", "document-1", principals=PRINCIPALS)
    assert edges and edges[0]["citations"]
    _assert_exact_citation(edges[0]["citations"][0])

    entity_citations = service.citations_for_party_or_client(
        "client", seeded["client"].id, PRINCIPALS
    )
    _assert_exact_citation(entity_citations[0])

    invoice_citations = _invoice_citations(
        service, [seeded["invoice"]], PRINCIPALS, require_all=True
    )
    _assert_exact_citation(invoice_citations[0])


def test_billing_citations_fail_closed_when_source_is_missing(
    session: Session, tmp_path
) -> None:
    service = RetrievalService(session, AppConfig(artifact_dir=tmp_path / "artifacts"))
    invoice = BillingInvoice(
        id="invoice-without-source",
        invoice_number="INV-UNSOURCED",
        invoice_total=5.0,
        currency="EUR",
    )
    session.add(invoice)
    session.commit()

    try:
        _invoice_citations(service, [invoice], PRINCIPALS, require_all=True)
    except RuntimeError as exc:
        assert "exact source provenance is missing" in str(exc)
    else:
        raise AssertionError("unsourced billing evidence must be withheld")


def test_mcp_query_tools_preserve_citations(factory, tmp_path) -> None:
    with factory() as session:
        _seed_cited_document(session)

    store = ConfigStore(tmp_path / "config.json")
    store.save(_header_auth_config(tmp_path / "artifacts"))
    with TestClient(create_app(factory, store)) as client:
        document = _call_mcp(
            client,
            "get_document",
            {"document_id": "document-1"},
        )
        _assert_exact_citation(document["citations"][0])

        matters = _call_mcp(client, "list_matters")
        _assert_exact_citation(matters[0]["citations"][0])

        edges = _call_mcp(
            client,
            "traverse",
            {"entity_type": "document", "entity_id": "document-1"},
        )
        _assert_exact_citation(edges[0]["citations"][0])

        rollup = _call_mcp(client, "billing_rollup", {"matter_id": "matter-1"})
        _assert_exact_citation(rollup["citations"][0])

        invoices = _call_mcp(client, "list_invoices", {"matter_id": "matter-1"})
        _assert_exact_citation(invoices[0]["citations"][0])

        entities = _call_mcp(client, "resolve_entity", {"query": "Citation GmbH"})
        _assert_exact_citation(entities[0]["citations"][0])

        decisions = _call_mcp(
            client,
            "search_decisions",
            {"query": "limits exposure"},
        )
        _assert_exact_citation(decisions[0]["citations"][0])

        scope = _call_mcp(client, "preview_search_scope")
        assert scope["project_ids"] == ["project-1"]
        assert scope["document_ids"] == ["document-1"]
        _assert_exact_citation(scope["citations"][0])


def test_mcp_document_reads_are_paginated_and_related_docs_are_discoverable(
    factory, tmp_path
) -> None:
    artifact_dir = tmp_path / "artifacts"
    with factory() as session:
        _seed_downloadable_pair(session, artifact_dir)

    store = ConfigStore(tmp_path / "config.json")
    store.save(_header_auth_config(artifact_dir))
    with TestClient(create_app(factory, store)) as client:
        page = _call_mcp(
            client,
            "get_document",
            {"document_id": "download-document", "offset": 5, "max_chars": 10},
        )
        assert page["content"] == {"text": "56789abcde"}
        assert page["content_page"] == {
            "offset": 5,
            "returned_chars": 10,
            "total_chars": 36,
            "has_more": True,
            "next_offset": 15,
        }

        related = _call_mcp(
            client,
            "find_related_documents",
            {"document_id": "download-document"},
        )
        assert related["result_count"] == 1
        item = related["related_documents"][0]
        assert item["document_id"] == "related-document"
        assert {reason["basis"] for reason in item["relationships"]} == {
            "stored_relation",
            "shared_matter",
        }
        assert item["citations"][0]["source_objects"][0]["path"] == "DL-001/related.docx"


def test_mcp_download_returns_exact_original_blob_and_short_lived_workspace_link(
    factory, tmp_path
) -> None:
    artifact_dir = tmp_path / "artifacts"
    with factory() as session:
        original, content_hash = _seed_downloadable_pair(session, artifact_dir)

    store = ConfigStore(tmp_path / "config.json")
    store.save(_header_auth_config(artifact_dir))
    headers = {
        "accept": "application/json, text/event-stream",
        "x-ki-principals": "group:citation-test",
    }
    with TestClient(create_app(factory, store)) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": "download",
                "method": "tools/call",
                "params": {
                    "name": "download_document",
                    "arguments": {"document_id": "download-document"},
                },
            },
            headers=headers,
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is False
        metadata = result["structuredContent"]
        assert metadata["filename"] == "original.docx"
        assert (
            metadata["mime_type"]
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert metadata["content_hash"] == content_hash
        assert metadata["citations"][0]["document"]["id"] == "download-document"
        assert any(block["type"] == "resource_link" for block in result["content"])

        parsed = urlsplit(metadata["download_url"])
        downloaded = client.get(parsed.path)
        assert downloaded.status_code == 200
        assert downloaded.content == original
        assert downloaded.headers["cache-control"] == "private, no-store"

        inline = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": "download-inline",
                "method": "tools/call",
                "params": {
                    "name": "download_document",
                    "arguments": {
                        "document_id": "download-document",
                        "inline_blob": True,
                    },
                },
            },
            headers=headers,
        ).json()["result"]
        resource = next(block["resource"] for block in inline["content"] if block["type"] == "resource")
        assert base64.b64decode(resource["blob"]) == original
        assert hashlib.sha256(original).hexdigest() == content_hash
