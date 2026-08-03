"""Granular MCP retrieval tools with identity-derived ACL enforcement."""

from __future__ import annotations

import base64
import hashlib
import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from fastmcp.tools.tool import ToolResult
from mcp.types import BlobResourceContents, EmbeddedResource, ResourceLink, TextContent
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.mcp_auth import resolve_mcp_identity
from knowledge_index.db.models import AuditEvent, BillingInvoice
from knowledge_index.downloads import DownloadTokenStore
from knowledge_index.retrieval import RetrievalService, SearchFilters
from knowledge_index.permissions import AccessService
from knowledge_index.taxonomies import (
    TaskType,
)


def create_mcp_server(
    session_factory: sessionmaker[Session],
    config_provider: Callable[[], AppConfig],
    download_tokens: DownloadTokenStore | None = None,
) -> FastMCP:
    download_store = download_tokens or DownloadTokenStore()
    mcp = FastMCP(
        "Knowledge Index",
        instructions=(
            "This is the primary source for firm, DMS, indexed, precedent, playbook, and "
            "matter-document questions, including documents that are not in the local "
            "workspace. Search here before asking the user to locate a firm document. Start "
            "with one focused search_semantic call (normally 5-8 results), then constrain by "
            "matter_id with search_filter. Whenever the user asks for related/linked files or "
            "a document graph, call find_related_documents; do not rely on semantic similarity "
            "alone. Use get_document only to read text and download_document to copy the exact "
            "original binary into the client workspace. Results are restricted to the caller "
            "identity. Every evidence-bearing result contains citations. Cite the exact "
            "citations[].project.id (or explicitly say no project), citations[].document.id, "
            "citations[].version.id, and citations[].source_objects[].id/path. Never make a "
            "factual claim from a result without a non-empty citations array."
        ),
        mask_error_details=False,
    )

    def service() -> tuple[Session, RetrievalService]:
        session = session_factory()
        return session, RetrievalService(session, config_provider())

    # Every tool below carries a ``title`` and a ``tags`` set as well as its description.
    # The description is written for a model and runs to a paragraph; the title is the one
    # short line the admin console prints, and the tag is the badge next to it. They live
    # here rather than in the console because the console's hand-maintained copy of this
    # list had drifted five tools behind what the server registers — see GET /api/mcp/tools.
    @mcp.tool(
        title="Exact metadata search",
        tags={"read"},
        description=(
            "List documents using exact legal metadata filters. Use before semantic search "
            "when the matter, document type, status, language, or date range is known. Each "
            "hit includes exact project, document-version, and source-object citations. "
            "only_final=true restricts to authoritative final/executed versions; the default "
            "searches every version including drafts and redlines. identifier is an EXACT "
            "match on a legal identifier (case number, Aktenzeichen, HRB, statute ref). "
            "party matches a resolved party_id or a party's exact canonical name. chunk_kind "
            "restricts to 'chunk' (document body), 'profile', or 'clause' chunks. "
            "practice_area filters by an Area of Law ontology node id with SUBTREE semantics "
            "(a parent area matches its children) — it restricts to documents in matters of "
            "that practice area."
        )
    )
    def search_filter(
        project_id: str | None = None,
        matter_id: str | None = None,
        doc_type: str | None = None,
        version_status: str | None = None,
        language: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        only_final: bool = False,
        identifier: str | None = None,
        party: str | None = None,
        chunk_kind: str | None = None,
        clause_type: str | None = None,
        practice_area: str | None = None,
        limit: int = 20,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> list[dict]:
        with audited_call(
            session_factory, "mcp.search_filter", headers, config_provider=config_provider
        ) as (principals, audit):
            session, retrieval = service()
            try:
                filters = SearchFilters(
                    project_id=project_id,
                    matter_id=matter_id,
                    doc_type=doc_type,
                    version_status=version_status,
                    language=language,
                    date_from=_parse_datetime(date_from),
                    date_to=_parse_datetime(date_to),
                    only_final=only_final,
                    identifier=identifier,
                    party=party,
                    clause_type=clause_type,
                    chunk_kind=chunk_kind or ("clause" if clause_type else None),
                    practice_area=practice_area,
                )
                result = [
                    hit.as_dict()
                    for hit in retrieval.search_filter(
                        principals=principals, filters=filters, limit=min(limit, 100)
                    )
                ]
                audit.update(result_count=len(result), filters=_active_filters(filters))
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Hybrid semantic and lexical ranking",
        tags={"read"},
        description=(
            "PRIMARY ENTRY POINT for questions about firm/DMS/indexed documents, especially "
            "when the file is not present in the caller's workspace. Run this before asking "
            "the user where an indexed document is. Hybrid semantic and lexical search over "
            "document chunks with ACL filtering before ranking. Prefer 5-8 focused results "
            "instead of repeated broad 20-result searches. Each hit includes exact project, "
            "document-version, source-object, and matched-chunk citations. only_final=true "
            "restricts to authoritative final/executed versions; the default searches every "
            "version including drafts and redlines. identifier is an EXACT match on a legal "
            "identifier; party matches a resolved party_id or exact canonical name; chunk_kind "
            "restricts to 'chunk' (body), 'profile', or 'clause' chunks. practice_area filters "
            "by an Area of Law ontology node id with SUBTREE semantics (a parent area matches "
            "its children), restricting to documents in matters of that practice area."
        )
    )
    def search_semantic(
        query: str,
        project_id: str | None = None,
        matter_id: str | None = None,
        doc_type: str | None = None,
        version_status: str | None = None,
        language: str | None = None,
        only_final: bool = False,
        identifier: str | None = None,
        party: str | None = None,
        chunk_kind: str | None = None,
        clause_type: str | None = None,
        practice_area: str | None = None,
        limit: int = 8,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> list[dict]:
        with audited_call(
            session_factory,
            "mcp.search_semantic",
            headers,
            config_provider=config_provider,
        ) as (
            principals,
            audit,
        ):
            session, retrieval = service()
            try:
                filters = SearchFilters(
                    project_id=project_id,
                    matter_id=matter_id,
                    doc_type=doc_type,
                    version_status=version_status,
                    language=language,
                    only_final=only_final,
                    identifier=identifier,
                    party=party,
                    clause_type=clause_type,
                    chunk_kind=chunk_kind or ("clause" if clause_type else None),
                    practice_area=practice_area,
                )
                result = [
                    hit.as_dict()
                    for hit in retrieval.search_semantic(
                        query, principals=principals, filters=filters, limit=min(limit, 100)
                    )
                ]
                audit.update(
                    result_count=len(result),
                    filters=_active_filters(filters),
                    **_query_fingerprint(query),
                )
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Read one document as paginated text",
        tags={"read"},
        description=(
            "Read one authorized document version as compact text with exact citations. Use "
            "document_id/version_id from search results. Text is paginated to avoid tool-output "
            "truncation; continue with content_page.next_offset when has_more is true. Do NOT "
            "use this to download or reconstruct Word/Excel/PDF files—call download_document "
            "for the exact original binary. Structured Docling metadata is omitted unless "
            "include_structured_metadata is explicitly requested."
        )
    )
    def get_document(
        document_id: str,
        version_id: str | None = None,
        offset: int = 0,
        max_chars: int = 30_000,
        include_structured_metadata: bool = False,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict | None:
        with audited_call(
            session_factory,
            "mcp.get_document",
            headers,
            config_provider=config_provider,
            target_type="document",
            target_id=document_id,
        ) as (principals, audit):
            session, retrieval = service()
            try:
                result = retrieval.get_document(
                    document_id, principals=principals, version_id=version_id
                )
                audit["found"] = result is not None
                if result is None:
                    return None
                if offset < 0:
                    raise ValueError("offset must be non-negative")
                if not 1 <= max_chars <= 50_000:
                    raise ValueError("max_chars must be between 1 and 50000")
                payload = result.get("content")
                if not isinstance(payload, dict):
                    result["content_page"] = {
                        "offset": offset,
                        "returned_chars": 0,
                        "total_chars": 0,
                        "has_more": False,
                        "next_offset": None,
                    }
                    return result
                text = str(payload.get("text") or "")
                end = min(len(text), offset + max_chars)
                compact_content = {"text": text[offset:end]}
                if include_structured_metadata:
                    compact_content.update(
                        {key: value for key, value in payload.items() if key != "text"}
                    )
                result["content"] = compact_content
                result["content_page"] = {
                    "offset": offset,
                    "returned_chars": max(0, end - offset),
                    "total_chars": len(text),
                    "has_more": end < len(text),
                    "next_offset": end if end < len(text) else None,
                }
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Export the original file",
        tags={"read"},
        description=(
            "Download/export/copy one document into the current client workspace as the exact "
            "original Word, Excel, PDF, email, or other binary—never a text reconstruction. "
            "Call this whenever the user says download, save, copy, export, or put a document "
            "in the workspace. By default it returns a short-lived ResourceLink plus a safe "
            "curl command; immediately run that command from the workspace. Set inline_blob "
            "only when the MCP client can directly materialize embedded BlobResourceContents. "
            "The result includes SHA-256, size, MIME type, filename, and exact citations."
        )
    )
    def download_document(
        document_id: str,
        version_id: str | None = None,
        source_object_id: str | None = None,
        inline_blob: bool = False,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> ToolResult:
        with audited_call(
            session_factory,
            "mcp.download_document",
            headers,
            config_provider=config_provider,
            target_type="document",
            target_id=document_id,
        ) as (principals, audit):
            session, retrieval = service()
            try:
                downloadable = retrieval.get_downloadable_document(
                    document_id,
                    principals=principals,
                    version_id=version_id,
                    source_object_id=source_object_id,
                )
                if downloadable is None:
                    audit["found"] = False
                    raise ValueError(
                        "document/version is absent, inaccessible, or its original blob is unavailable"
                    )
                audit.update(
                    found=True,
                    version_id=downloadable.version_id,
                    source_object_id=downloadable.source_object_id,
                    size_bytes=downloadable.size_bytes,
                )
                capability = download_store.issue(
                    document_id=downloadable.document_id,
                    version_id=downloadable.version_id,
                    source_object_id=downloadable.source_object_id,
                    content_hash=downloadable.content_hash,
                    filename=downloadable.filename,
                    mime_type=downloadable.mime_type,
                    size_bytes=downloadable.size_bytes,
                    principals=principals,
                )
                base_url = _download_base_url(headers)
                encoded_name = quote(downloadable.filename, safe="")
                download_url = (
                    f"{base_url}/api/downloads/{capability.token}/{encoded_name}"
                )
                save_command = (
                    "curl --fail --location --output "
                    f"{shlex.quote(downloadable.filename)} {shlex.quote(download_url)}"
                )
                metadata = {
                    **downloadable.metadata(),
                    "download_url": download_url,
                    "expires_in_seconds": download_store.ttl_seconds,
                    "save_command": save_command,
                    "delivery": "embedded_blob" if inline_blob else "resource_link",
                }
                content: list = [
                    TextContent(
                        type="text",
                        text=(
                            f"Exact original ready: {downloadable.filename} "
                            f"({downloadable.size_bytes} bytes, SHA-256 "
                            f"{downloadable.content_hash}). Run this from the current "
                            f"workspace now:\n{save_command}"
                        ),
                    ),
                    ResourceLink(
                        type="resource_link",
                        name=downloadable.filename,
                        title=downloadable.filename,
                        uri=download_url,
                        description="Short-lived link to the authorized original document blob.",
                        mimeType=downloadable.mime_type,
                        size=downloadable.size_bytes,
                    ),
                ]
                if inline_blob:
                    content.append(
                        EmbeddedResource(
                            type="resource",
                            resource=BlobResourceContents(
                                uri=(
                                    f"ki://documents/{downloadable.document_id}/versions/"
                                    f"{downloadable.version_id}/{encoded_name}"
                                ),
                                mimeType=downloadable.mime_type,
                                blob=base64.b64encode(
                                    downloadable.cached_path.read_bytes()
                                ).decode("ascii"),
                            ),
                        )
                    )
                return ToolResult(content=content, structured_content=metadata)
            finally:
                session.close()

    @mcp.tool(
        title="Relations, matter and thread context",
        tags={"read"},
        description=(
            "FIND RELATED DOCUMENTS for requests such as 'related docs', 'linked files', "
            "'relationship traversal', 'what belongs with this document', or 'render a "
            "document graph'. Prefer this over repeated semantic searches. It returns stored "
            "document relations plus independently labeled shared-thread and shared-matter "
            "context, graph-ready edges, and an exact citation for every visible document. "
            "Shared-matter context is not represented as an inferred legal reference."
        )
    )
    def find_related_documents(
        document_id: str,
        include_same_matter: bool = True,
        limit: int = 50,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict | None:
        with audited_call(
            session_factory,
            "mcp.find_related_documents",
            headers,
            config_provider=config_provider,
            target_type="document",
            target_id=document_id,
        ) as (principals, audit):
            session, retrieval = service()
            try:
                result = retrieval.find_related_documents(
                    document_id,
                    principals=principals,
                    include_same_matter=include_same_matter,
                    limit=min(max(limit, 0), 250),
                )
                audit.update(
                    found=result is not None,
                    result_count=result["result_count"] if result else 0,
                    include_same_matter=include_same_matter,
                )
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Walk stored relation edges",
        tags={"read"},
        description=(
            "LOW-LEVEL RELATIONSHIP TRAVERSAL. Use for exact stored graph edges such as "
            "supersedes, annex_of, references, responds_to, and belongs_to_thread. For the "
            "common user request 'show/find all related documents' or a document graph, call "
            "find_related_documents instead because it resolves document endpoints and also "
            "includes clearly labeled shared-matter/thread context. Each edge here includes "
            "exact citations for both authorized endpoints."
        )
    )
    def traverse(
        entity_type: str,
        entity_id: str,
        limit: int = 100,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> list[dict]:
        with audited_call(
            session_factory,
            "mcp.traverse",
            headers,
            config_provider=config_provider,
            target_type=entity_type,
            target_id=entity_id,
        ) as (principals, audit):
            session, retrieval = service()
            try:
                result = retrieval.traverse(
                    entity_type, entity_id, principals=principals, limit=min(limit, 250)
                )
                audit["result_count"] = len(result)
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Matters the caller can read",
        tags={"read"},
        description=(
            "List matters that contain at least one version visible to the caller, including "
            "the exact project and every authorized document-version/source-object citation. "
            "practice_area filters by an Area of Law ontology node id with SUBTREE semantics "
            "(a parent area matches its children) — find node ids via list_taxonomies."
        )
    )
    def list_matters(
        limit: int = 100,
        practice_area: str | None = None,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> list[dict]:
        with audited_call(
            session_factory, "mcp.list_matters", headers, config_provider=config_provider
        ) as (principals, audit):
            session, retrieval = service()
            try:
                result = retrieval.list_matters(
                    principals=principals, limit=min(limit, 250), practice_area=practice_area
                )
                audit.update(result_count=len(result), practice_area=practice_area)
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Invoiced total and fees per task code",
        tags={"read"},
        description=(
            "Roll up a matter's billing: invoiced total plus hours and fees per UTBMS task "
            "code. Restricted to matters the caller can see and fails closed if any invoice "
            "lacks an exact project/document/source-object citation."
        )
    )
    def billing_rollup(matter_id: str, headers: dict[str, str] = CurrentHeaders()) -> dict:
        with audited_call(
            session_factory, "mcp.billing_rollup", headers, config_provider=config_provider
        ) as (principals, audit):
            from knowledge_index.pipeline.billing import billing_rollup as _rollup

            session, retrieval = service()
            try:
                visible = {m["id"] for m in retrieval.list_matters(principals=principals, limit=1000)}
                if matter_id not in visible:
                    audit["result_count"] = 0
                    return {"matter_id": matter_id, "authorized": False}
                result = _rollup(session, matter_id)
                invoices = session.scalars(
                    select(BillingInvoice).where(BillingInvoice.matter_id == matter_id)
                ).all()
                citations = _invoice_citations(
                    retrieval, invoices, principals, require_all=True
                )
                result["project_ids"] = _citation_project_ids(citations)
                result["citations"] = citations
                audit["result_count"] = result.get("invoice_count", 0)
                return result
            finally:
                session.close()

    @mcp.tool(
        title="A matter's invoices",
        tags={"read"},
        description=(
            "List a matter's invoices (number, date, total). Restricted to matters the caller "
            "can see; every invoice includes its exact project/document/source-object citation."
        )
    )
    def list_invoices(matter_id: str, headers: dict[str, str] = CurrentHeaders()) -> list[dict]:
        with audited_call(
            session_factory, "mcp.list_invoices", headers, config_provider=config_provider
        ) as (principals, audit):
            session, retrieval = service()
            try:
                visible = {m["id"] for m in retrieval.list_matters(principals=principals, limit=1000)}
                if matter_id not in visible:
                    audit["result_count"] = 0
                    return []
                invoices = session.scalars(
                    select(BillingInvoice).where(BillingInvoice.matter_id == matter_id)
                ).all()
                result = []
                for invoice in invoices:
                    citations = _invoice_citations(
                        retrieval, [invoice], principals, require_all=True
                    )
                    result.append(
                        {
                            "id": invoice.id,
                            "matter_id": invoice.matter_id,
                            "project_ids": _citation_project_ids(citations),
                            "invoice_number": invoice.invoice_number,
                            "invoice_date": (
                                invoice.invoice_date.isoformat()
                                if invoice.invoice_date
                                else None
                            ),
                            "invoice_total": invoice.invoice_total,
                            "currency": invoice.currency,
                            "citations": citations,
                        }
                    )
                audit["result_count"] = len(result)
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Resolve a party or client name",
        tags={"read"},
        description=(
            "Resolve a party or client name or identifier (LEI, HRB, VAT) to the firm's known "
            "entities, including entities that share an identifier. Results without an exact "
            "authorized project/document/source-object citation are withheld."
        )
    )
    def resolve_entity(query: str, headers: dict[str, str] = CurrentHeaders()) -> list[dict]:
        with audited_call(
            session_factory, "mcp.resolve_entity", headers, config_provider=config_provider
        ) as (principals, audit):
            from knowledge_index.pipeline.billing import resolve_entity as _resolve

            session, retrieval = service()
            try:
                raw = _resolve(session, query)
                result = []
                for item in raw:
                    citations = retrieval.citations_for_party_or_client(
                        item["entity_type"], item["id"], principals
                    )
                    if not citations:
                        continue
                    result.append(
                        {
                            **item,
                            "project_ids": sorted(
                                {
                                    citation["project"]["id"]
                                    for citation in citations
                                    if citation.get("project")
                                }
                            ),
                            "citations": citations,
                        }
                    )
                audit.update(result_count=len(result), **_query_fingerprint(query))
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Search drafting and negotiation rationale",
        tags={"read"},
        description=(
            "Search anonymized drafting and negotiation rationale. Underlying evidence remains "
            "protected by the source document ACL. Every result includes the exact evidence "
            "project, document version, and source object citation."
        )
    )
    def search_decisions(
        query: str,
        limit: int = 20,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> list[dict]:
        with audited_call(
            session_factory,
            "mcp.search_decisions",
            headers,
            config_provider=config_provider,
        ) as (
            principals,
            audit,
        ):
            session, retrieval = service()
            try:
                result = retrieval.search_decisions(
                    query, principals=principals, limit=min(limit, 100)
                )
                audit.update(result_count=len(result), **_query_fingerprint(query))
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Document, task and practice-area vocabularies",
        tags={"read"},
        description=(
            "The active document-type ontology and the stable auxiliary taxonomies. "
            "Document types are a hierarchy, not a flat list: browse it with "
            "ontology_children or find a node with ontology_search, then pass the "
            "node id as the doc_type filter (it matches the node's whole subtree)."
        ),
    )
    def list_taxonomies(headers: dict[str, str] = CurrentHeaders()) -> dict:
        with audited_call(
            session_factory, "mcp.list_taxonomies", headers, config_provider=config_provider
        ) as (_principals, audit):
            config = config_provider()
            scope = config.doc_ontology()
            # Practice areas come from the live Area of Law facet (subtree-filterable
            # node ids for list_matters), not a hardcoded enum.
            try:
                area_scope = config.ontology_facet("area_of_law")
                practice_areas = [
                    {"id": node["id"], "label": node["label"], "children": node["children"]}
                    for root in area_scope.roots()
                    for node in area_scope.children(root["id"])
                ]
            except ValueError:
                practice_areas = []
            result = {
                "ontology": {
                    "artifact": scope.artifact.name,
                    "version": scope.artifact.version,
                    "fingerprint": scope.fingerprint,
                    "visible_nodes": len(scope.visible),
                    "roots": scope.roots(),
                },
                "task_types": [item.value for item in TaskType],
                "practice_areas": practice_areas,
            }
            audit["taxonomy_count"] = len(scope.visible)
            return result

    @mcp.tool(
        title="Search the document-type ontology",
        tags={"read"},
        description=(
            "Find document-type ontology nodes by name, synonym, or definition. Use the "
            "returned node id as the doc_type filter in search_filter/search_semantic — "
            "the filter matches the node AND everything below it (an interior node like "
            "'Agreements' covers every agreement type)."
        ),
    )
    def ontology_search(query: str, headers: dict[str, str] = CurrentHeaders()) -> list[dict]:
        with audited_call(
            session_factory, "mcp.ontology_search", headers, config_provider=config_provider
        ) as (_principals, audit):
            results = config_provider().doc_ontology().search(query, limit=12)
            audit.update(result_count=len(results), query=query)
            return results

    @mcp.tool(
        title="Ontology top-level branches",
        tags={"read"},
        description="Top-level branches of the active document-type ontology.",
    )
    def ontology_roots(headers: dict[str, str] = CurrentHeaders()) -> list[dict]:
        with audited_call(
            session_factory, "mcp.ontology_roots", headers, config_provider=config_provider
        ) as (_principals, audit):
            results = config_provider().doc_ontology().roots()
            audit["result_count"] = len(results)
            return results

    @mcp.tool(
        title="Descend the ontology one level",
        tags={"read"},
        description=(
            "Children of one ontology node, with definitions — descend the document-type "
            "hierarchy one level at a time. A child count of 0 marks a leaf."
        ),
    )
    def ontology_children(node_id: str, headers: dict[str, str] = CurrentHeaders()) -> list[dict]:
        with audited_call(
            session_factory, "mcp.ontology_children", headers, config_provider=config_provider
        ) as (_principals, audit):
            results = config_provider().doc_ontology().children(node_id)
            audit.update(result_count=len(results), node_id=node_id)
            return results

    @mcp.tool(
        title="Read one ontology node",
        tags={"read"},
        description="Full detail for one ontology node: definition, synonyms, path, parents.",
    )
    def ontology_node(node_id: str, headers: dict[str, str] = CurrentHeaders()) -> dict:
        with audited_call(
            session_factory, "mcp.ontology_node", headers, config_provider=config_provider
        ) as (_principals, audit):
            detail = config_provider().doc_ontology().node(node_id)
            audit["node_id"] = node_id
            return detail or {"error": f"unknown or inactive node {node_id!r}"}

    @mcp.tool(
        title="What the caller may see, before ranking",
        tags={"scope"},
        description=(
            "Compile the caller's ACL and optional project/document selections into the exact "
            "scope that will constrain hybrid/vector retrieval before scoring."
        ),
    )
    def preview_search_scope(
        project_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.preview_search_scope",
            headers,
            config_provider=config_provider,
        ) as (principals, audit):
            session = session_factory()
            try:
                scope = AccessService(session).compile_scope(
                    principals,
                    project_ids=project_ids or [],
                    document_ids=document_ids or [],
                )
                retrieval = RetrievalService(session, config_provider())
                result = {
                    "fingerprint": scope.fingerprint,
                    "project_count": len(scope.project_ids),
                    "document_count": len(scope.document_ids),
                    "project_ids": list(scope.project_ids),
                    "document_ids": list(scope.document_ids),
                    "projects": [
                        project
                        for project_id in scope.project_ids
                        if (project := retrieval.project_reference(project_id)) is not None
                    ],
                    "citations": _merge_citations(
                        [
                            citation
                            for document_id in scope.document_ids
                            for citation in retrieval.citations_for_document(
                                document_id, principals
                            )
                        ]
                    ),
                }
                audit.update(
                    fingerprint=scope.fingerprint,
                    project_count=len(scope.project_ids),
                    document_count=len(scope.document_ids),
                )
                return result
            finally:
                session.close()

    return mcp


def _invoice_citations(
    retrieval: RetrievalService,
    invoices: list[BillingInvoice],
    principals: set[str],
    *,
    require_all: bool,
) -> list[dict]:
    citations: list[dict] = []
    missing: list[str] = []
    for invoice in invoices:
        invoice_citations = retrieval.citations_for_source_object(
            invoice.source_object_id, principals
        )
        if not invoice_citations:
            missing.append(invoice.id)
            continue
        citations.extend(invoice_citations)
    if require_all and missing:
        raise RuntimeError(
            "billing result withheld because exact source provenance is missing for invoice(s): "
            + ", ".join(sorted(missing))
        )
    return _merge_citations(citations)


def _citation_project_ids(citations: list[dict]) -> list[str]:
    return sorted(
        {
            citation["project"]["id"]
            for citation in citations
            if citation.get("project")
        }
    )


def _merge_citations(citations: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result: list[dict] = []
    for citation in citations:
        document = citation.get("document") or {}
        version = citation.get("version") or {}
        source_ids = tuple(
            sorted(
                source["id"]
                for source in citation.get("source_objects") or []
                if source.get("id")
            )
        )
        key = (document.get("id"), version.get("id"), source_ids)
        if key in seen:
            continue
        seen.add(key)
        result.append(citation)
    return result


def principals_from_headers(headers: dict[str, str], config: AppConfig) -> set[str]:
    """Resolve the caller of an MCP tool to the principals its results are scoped to.

    The config is required rather than optional: the old optional form fell back to
    trusting ``x-ki-principals`` outright, so a caller that reached the transport chose
    its own identity. Every tool result is an access-control decision, so there is no
    configuration-free default that is safe to have.
    """

    return set(resolve_mcp_identity(headers, config).principals)


@contextmanager
def audited_call(
    session_factory: sessionmaker[Session],
    action: str,
    headers: dict[str, str],
    *,
    config_provider: Callable[[], AppConfig],
    target_type: str | None = None,
    target_id: str | None = None,
) -> Iterator[tuple[set[str], dict]]:
    """Fail closed if a tool invocation cannot be written to the audit ledger."""
    try:
        principals = principals_from_headers(headers, config_provider())
    except PermissionError as exc:
        _write_audit(
            session_factory,
            action=action,
            principals=[],
            outcome="denied",
            target_type=target_type,
            target_id=target_id,
            details={"reason": str(exc)},
        )
        raise

    details: dict = {}
    try:
        yield principals, details
    except Exception as exc:
        details["error_class"] = type(exc).__name__
        _write_audit(
            session_factory,
            action=action,
            principals=principals,
            outcome="error",
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        raise
    else:
        _write_audit(
            session_factory,
            action=action,
            principals=principals,
            outcome="success",
            target_type=target_type,
            target_id=target_id,
            details=details,
        )


def _write_audit(
    session_factory: sessionmaker[Session],
    *,
    action: str,
    principals: set[str] | list[str],
    outcome: str,
    target_type: str | None,
    target_id: str | None,
    details: dict,
) -> None:
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_principals=sorted(principals),
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome=outcome,
                details=details,
            )
        )
        session.commit()


def _query_fingerprint(query: str) -> dict:
    """Audit query use without persisting privileged query text."""
    encoded = query.encode("utf-8")
    return {"query_sha256": hashlib.sha256(encoded).hexdigest(), "query_chars": len(query)}


def _download_base_url(headers: dict[str, str]) -> str:
    """Reconstruct the caller-visible origin for a short-lived download link."""

    normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
    scheme = normalized.get("x-forwarded-proto", "http").split(",", 1)[0].strip()
    if scheme not in {"http", "https"}:
        scheme = "http"
    host = (
        normalized.get("x-forwarded-host")
        or normalized.get("host")
        or "127.0.0.1:8000"
    ).split(",", 1)[0].strip()
    if not host or any(character in host for character in "/\\\r\n"):
        host = "127.0.0.1:8000"
    return f"{scheme}://{host}"


def _active_filters(filters: SearchFilters) -> dict:
    active = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in vars(filters).items()
        if value is not None
    }
    # only_final defaults False on every call; record it only when it actually
    # narrows the search, to avoid a constant noise column on every audit row.
    if not filters.only_final:
        active.pop("only_final", None)
    return active


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
