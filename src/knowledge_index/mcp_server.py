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
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.mcp_auth import resolve_mcp_identity
from knowledge_index.db.models import AuditEvent, BillingInvoice
from knowledge_index.downloads import DownloadTokenStore
from knowledge_index.retrieval import RetrievalService, SearchFilters
from knowledge_index.retrieval_types import Page
from knowledge_index.permissions import AccessService
from knowledge_index.taxonomies import (
    TaskType,
)

# Appended verbatim to every list-shaped tool's description. One identical
# paragraph across the surface is deliberate: a model that learns the contract
# once applies it everywhere, whereas per-tool phrasing invites the reader to
# assume each tool differs. The last sentence is the one that matters — a full
# page used to be indistinguishable from the end of the corpus, and callers
# routinely concluded a matter was empty because the first page was all they
# ever asked for.
_PAGINATION_CONTRACT = (
    " PAGINATION: returns {results, page}, not a bare list. page = {offset, limit, "
    "returned, has_more, next_offset, and total where it can be counted exactly}. "
    "When page.has_more is true there ARE more matches: call again with "
    "offset=page.next_offset and identical filters to get the next page. A full "
    "page is never evidence that the result set ended — only has_more=false is. "
    "Never state or imply a count, a total, or 'that is all of them' from a single "
    "page unless page.has_more is false or page.total says otherwise."
)

# Same idea, for the handful of tools whose result really is complete.
_COMPLETE_RESULT = (
    " NOT PAGINATED: this returns the complete set in one call, so the result is "
    "exhaustive and may be treated as a total."
)


# Which filter parameter takes an id from each facet. This is the whole reason a
# node carries its facet: the ids look identical, they are taken by four
# different parameters, and passing one to the wrong parameter matches nothing
# and says nothing. Returned beside every node so the caller never has to guess.
_FACET_FILTER = {
    "doc_type": "doc_type",
    "area_of_law": "practice_area",
    "service": "matter_kind",
    "clause": "clause_type",
}


def _with_filter(node: dict) -> dict:
    """A node plus the name of the filter its id belongs in."""
    facet = node.get("facet")
    return {**node, "use_as_filter": _FACET_FILTER.get(facet)} if facet else node


# How large an original may be before ``download_document`` stops volunteering the
# bytes to a caller that did not ask for them either way.
#
# The blob is charged to whoever holds the result, and for a model that is context
# spent on something it cannot read: base64 costs four characters per three bytes,
# and a document original is not text (a .docx is a zip). Measured on the hosted
# demo, one 68 KB agreement is 2.5 KB of result without the blob and 95 KB with it.
# 32 KB is around 44 KB encoded, roughly 11k tokens — about as much as a tool
# result should ever cost. Above it the caller gets the link, which is the answer
# the tool gave everyone before the bytes were added.
_INLINE_BLOB_AUTO_MAX_BYTES = 32 * 1024


def _paged(page: Page, **extra) -> dict:
    """The response envelope every paginated tool returns."""
    return {"results": page.items, "page": page.as_dict(), **extra}


def _page_bounds(limit: int, offset: int, *, max_limit: int) -> tuple[int, int]:
    """Validate a page request, clamping the limit and rejecting nonsense.

    The limit is clamped rather than rejected because the effective value is
    echoed back in ``page.limit`` and ``has_more`` stays exact — a caller that
    asked for 5,000 rows gets a smaller page and an explicit way to continue,
    which is strictly better than an error. A negative offset or limit is a bug
    in the caller and is refused outright.
    """
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, max_limit), offset


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
            "alone. Once you have the right document, search_in_document ranks that one "
            "document's own passages against your question and is the way in; get_document "
            "reads it through a page at a time, which is what an enumeration — or a claim "
            "that something is absent — needs. download_document copies the exact original "
            "binary into the client workspace. Results are restricted to the caller "
            "identity. LIST ROWS CARRY THE DOCUMENT'S METADATA, NOT ITS EVIDENCE RECORD: "
            "search hits give you title, doc_type_label, doc_date, version_status, the "
            "matched excerpt, matched_identifiers, score and source_paths, plus "
            "document_id/version_id/matter_id. The exact citation record "
            "(project, document-version, source objects) comes from get_document / "
            "download_document on the item you rely on. Never make a factual claim about a "
            "document's contents without having read it through one of those. "
            "EVERY LIST-SHAPED TOOL IS PAGINATED and returns {results, page} rather than a "
            "bare list; page.has_more=true means more matches exist, and the next page is "
            "the same call with offset=page.next_offset. One page is a sample, not an "
            "inventory: before answering a question that turns on completeness — how many, "
            "every, all, none, the full list, is X absent — either page until has_more is "
            "false or say plainly which part of the set you looked at. Tools whose "
            "description says NOT PAGINATED already return everything."
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
            "List documents using exact legal metadata filters. This is the ENUMERATION "
            "tool: one row per document version, ordered by date, complete. Use it — not "
            "semantic search — for 'what is in this matter', 'which documents of this "
            "type', 'how many', and any question whose answer is a set. "
            "contains_all_terms=[...] adds an exact-phrase CONTENT filter with the "
            "same complete-set semantics: only documents whose text contains EVERY "
            "listed phrase come back, and the phrases may sit in different parts of "
            "the document. This is the tool for 'every document (or matter) that "
            "mentions both X and Y' — semantic search RANKS a top-k and can lose a "
            "rare exact match under commoner ones; this FILTERS the full set and "
            "pages it, so an absence claim can stand on it. Phrases match through "
            "the lexical analyzer (inflected forms match; a phrase must be real "
            "words, not a substring), and it ANDs with every other filter here — "
            "scope it to a matter, a doc_type or a practice, and group the rows by "
            "matter_ref for per-matter answers. Each "
            "hit carries the document's metadata (title, doc_type_label, doc_date, "
            "matter_ref, parties with roles, identifiers, version_status, source_paths) "
            "and ids; ask search_in_document for the passage that answers your question, or "
            "get_document to read a hit through and for its citation record. "
            "Each row is ONE VERSION of a document: version_ordinal orders the versions "
            "(1 = earliest), is_latest_final says whether this row IS the authoritative "
            "one, and latest_final_version_id names the version that is when it is not. "
            "Rows sharing a document_id are versions of the same document — cite the "
            "authoritative one unless the question is about the negotiation history. "
            "Rows carry no excerpt or score: no query ran, so there is nothing to excerpt "
            "or rank — open a document to see what it says. When page.has_more is false, "
            "page.total is the exact number of matching documents. "
            "STATUS ANSWERS WHETHER, NOT WHAT. version_status, only_final and lifecycle tell "
            "you whether something is operative or whether it happened. They do not tell "
            "you what kind of thing it is, and using them as a proxy for kind is a "
            "recurring way to undercount: an unsigned agreement is weak evidence that a "
            "deal closed and strong evidence of what the parties were agreeing to, so it "
            "still settles the structure, the security, the terms. Exclude unexecuted "
            "versions when the question turns on OCCURRENCE — did it close, was it "
            "signed, what is in force today. Read them like any other document when the "
            "question asks what a matter or a deal IS. "
            "only_final=true restricts to authoritative final/executed versions; the default "
            "searches every version including drafts and redlines. identifier is an EXACT "
            "match on a legal identifier (case number, Aktenzeichen, HRB, statute ref). "
            "party matches a resolved party_id or a party's exact canonical name. chunk_kind "
            "restricts to 'chunk' (document body), 'profile', or 'clause' chunks. "
            "SCOPE A SEARCH TO A PRACTICE, A LAWYER OR A STATE: practice_group=... searches only matters that group works, firm_person=... only a given lawyer's, and lifecycle=... only matters in one state. These are the SAME filters list_matters takes and they resolve identically — spelling, ampersands and a surname all land where you mean — so a question about one practice's work is one scoped call rather than a search over the estate followed by a manual sift, and the matters you searched are exactly the matters that listing would show. They AND with practice_area, matter_id, doc_type and the rest. "
            "practice_area filters by an Area of Law ontology node id with SUBTREE semantics "
            "(a parent area matches its children) — it restricts to documents in matters of "
            "that practice area. matter_kind, practice_area and the like narrow WHERE you "
            "look, and that is all they do: they are filing labels, not findings. Never "
            "cite one as evidence of what a document or matter is, and never leave a "
            "candidate out of an answer on a label alone — confirm from the text. "
            "Results are ordered by document date, newest first, so "
            "paging through them walks the matter's timeline backwards."
            + _PAGINATION_CONTRACT
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
        matter_kind: str | None = None,
        practice_group: str | None = None,
        firm_person: str | None = None,
        lifecycle: str | None = None,
        contains_all_terms: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory, "mcp.search_filter", headers, config_provider=config_provider
        ) as (principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=100)
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
                    matter_kind=matter_kind,
                    practice_group=practice_group,
                    firm_person=firm_person,
                    lifecycle=lifecycle,
                    contains_all_terms=contains_all_terms,
                )
                found = retrieval.search_filter_page(
                    principals=principals, filters=filters, limit=limit, offset=offset
                )
                page = Page(
                    # No query ran, so score/excerpt/matched_identifiers would be
                    # 0.0, a first-320-characters slice and [] on every row.
                    items=[hit.as_dict(include_match=False) for hit in found.items],
                    offset=found.offset,
                    limit=found.limit,
                    has_more=found.has_more,
                    total=found.total,
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                    filters=_active_filters(filters),
                )
                return _paged(
                    page, **_empty_result_help(page, retrieval, principals, filters)
                )
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
            "the document's metadata (title, doc_type_label, doc_date, matter_ref, parties "
            "with roles, identifiers, version_status, excerpt, source_paths) and ids; "
            "an excerpt marks where the match happened, not where the answer is — "
            "search_in_document reaches the passage inside a hit, get_document reads "
            "it through and carries its citation record. Each row is ONE "
            "VERSION: version_ordinal orders them, is_latest_final marks the "
            "authoritative one, latest_final_version_id names it when this row is not, "
            "and rows sharing a document_id are versions of one document. STATUS ANSWERS "
            "WHETHER, NOT WHAT: version_status, only_final and lifecycle say whether "
            "something is operative or whether it happened, never what kind of thing it "
            "is. An unsigned agreement is weak evidence a deal closed and strong evidence "
            "of what the parties were agreeing to, so it still settles the structure and "
            "the terms. Exclude unexecuted versions when the question turns on occurrence; "
            "read them like any other document when it asks what a matter or deal IS. "
            "only_final=true "
            "restricts to authoritative final/executed versions; the default searches every "
            "version including drafts and redlines. identifier is an EXACT match on a legal "
            "identifier; party matches a resolved party_id or exact canonical name; chunk_kind "
            "restricts to 'chunk' (body), 'profile', or 'clause' chunks. practice_area filters "
            "by an Area of Law ontology node id with SUBTREE semantics (a parent area matches "
            "its children), restricting to documents in matters of that practice area. "
            "SCOPE A SEARCH TO A PRACTICE, A LAWYER OR A STATE: practice_group=... "
            "searches only the matters that group works, firm_person=... only a given "
            "lawyer's, lifecycle=... only matters in one state. These are the SAME filters list_matters takes and they "
            "resolve identically — spelling, ampersands, a surname or an initial all land "
            "where you mean — so the matters you just searched are exactly the matters that "
            "listing would show. A question about how one practice usually drafts something is "
            "one scoped call instead of a search across the whole estate followed by a "
            "manual sift, and scoping first is also what stops a strong hit from another "
            "practice being cited into an answer about this one. They AND with "
            "practice_area, matter_id, doc_type and the rest. All of these narrow the "
            "search and that is all they do: a filter label is a filing decision, not "
            "evidence — what a document IS or says comes from the document itself. "
            "Results are ranked by relevance, so paging returns steadily weaker matches: "
            "when a page stops answering the question, narrow the filters instead of "
            "paging further. Each page re-ranks the whole window, so offset + limit must "
            "stay within 500 and pages are only stable while the index is unchanged. "
            "This tool RANKS; it does not enumerate. When the answer is a set — which "
            "matters, how many, list every document of a kind, everything in a matter — "
            "use search_filter, which returns one row per document with an exact total. "
            "A relevance page is a sample and never an inventory, however full it looks."
            + _PAGINATION_CONTRACT
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
        matter_kind: str | None = None,
        practice_group: str | None = None,
        firm_person: str | None = None,
        lifecycle: str | None = None,
        limit: int = 8,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.search_semantic",
            headers,
            config_provider=config_provider,
        ) as (
            principals,
            audit,
        ):
            limit, offset = _page_bounds(limit, offset, max_limit=100)
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
                    matter_kind=matter_kind,
                    practice_group=practice_group,
                    firm_person=firm_person,
                    lifecycle=lifecycle,
                )
                found = retrieval.search_semantic_page(
                    query,
                    principals=principals,
                    filters=filters,
                    limit=limit,
                    offset=offset,
                )
                page = Page(
                    items=[hit.as_dict() for hit in found.items],
                    offset=found.offset,
                    limit=found.limit,
                    has_more=found.has_more,
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                    filters=_active_filters(filters),
                    **_query_fingerprint(query),
                )
                return _paged(
                    page, **_empty_result_help(page, retrieval, principals, filters)
                )
            finally:
                session.close()

    @mcp.tool(
        title="Read one document, one page of chunks at a time",
        tags={"read"},
        description=(
            "Read one authorized document version through, a page at a time, with exact "
            "citations. This is the THOROUGH read, and it is the right tool for two jobs: "
            "establishing that a document does NOT contain something, and enumerating "
            "everything of some kind in it. For a specific question about a document — where "
            "a clause is, what it says, whether a provision is present — call "
            "search_in_document first; it reaches the passage without the pages in front of "
            "it, and tells you which page to come here for if you want the surroundings. "
            "Use document_id/version_id from search results. Do NOT use this to download or "
            "reconstruct Word/Excel/PDF files—call download_document for the exact original "
            "binary. PAGINATION IS BY CHUNK, the same numbered units search ranks: with the "
            "default page 1 is chunks 0-11, page 2 is 12-23, and so on. The reply's `page` "
            "block states where you are — {page, pages, first_chunk, last_chunk, "
            "total_chunks, has_more, next_page} — and you continue with page=page.next_page. "
            "A page is a PREFIX of the document, not the document: while has_more is true "
            "there is text you have not seen, so no claim that something is absent can be "
            "made from it."
        )
    )
    def get_document(
        document_id: str,
        version_id: str | None = None,
        page: int = 1,
        chunks_per_page: int = 12,
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
                paged = retrieval.document_chunks(
                    document_id,
                    principals=principals,
                    version_id=version_id,
                    page=page,
                    chunks_per_page=chunks_per_page,
                )
                if paged is None:
                    return None
                audit["page"] = page
                body = "\n\n".join(chunk["text"] for chunk in paged["chunks"])
                info = paged["page"]
                if info["has_more"]:
                    # The marker goes in the TEXT, not only in the page block. A
                    # model given the first window of a long agreement will report
                    # that a provision is absent from it, having read a fraction of
                    # it: the structured field saying otherwise sits beside prose
                    # that reads complete. A field can be skimmed past; a sentence
                    # where the text stops cannot.
                    body += (
                        f"\n\n[PAGE {info['page']} OF {info['pages']} — chunks "
                        f"{info['first_chunk']}–{info['last_chunk']} of "
                        f"{info['total_chunks']}. This is a PREFIX of the document. "
                        f"If you are looking for something specific, "
                        f"search_in_document(query=…) reaches it directly and names the "
                        f"page it is on. If you need the whole document — to enumerate "
                        f"it, or to establish that something is NOT in it — continue "
                        f"with get_document(page={info['next_page']}); that claim cannot "
                        f"be made from a prefix.]"
                    )
                result["content"] = {"text": body}
                result["chunks"] = paged["chunks"]
                result["page"] = info
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Search inside one document",
        tags={"read"},
        description=(
            "THE FIRST CALL to make on a document you have identified: rank that one "
            "document's own passages against your question and read only what answers it. "
            "A document runs to many pages, and most questions turn on one or two of them — "
            "'where does this agreement cap indemnity', 'does this credit agreement have a "
            "springing lien', 'what does the termination clause say'. Ask here first, then "
            "widen with get_document only if what comes back is not enough. "
            "Every result carries the chunk `ordinal`, the passage text, `get_document_page` "
            "— the page of get_document holding it — and `context_pages` {previous, this, "
            "next}, so reading around a hit is one call and cannot run off either end. Page "
            "numbers are computed against `chunks_per_page` (default 12); pass the same value "
            "to get_document and they address exactly the text the passage came from. "
            "Returns up to `limit` passages (default 5) with {offset, limit, has_more, "
            "next_offset}; page on with offset=page.next_offset for more. "
            "WHAT IT CANNOT DO: a ranked search shows that something IS present, never that "
            "it is absent, and it does not enumerate. If your answer is a claim that a "
            "document lacks something, or a list of everything of some kind in it, that "
            "needs a full read via get_document. "
            "This searches inside ONE document; search_semantic searches the estate and tells "
            "you which document to open."
        )
    )
    def search_in_document(
        document_id: str,
        query: str,
        version_id: str | None = None,
        limit: int = 5,
        offset: int = 0,
        chunks_per_page: int = 12,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict | None:
        with audited_call(
            session_factory,
            "mcp.search_in_document",
            headers,
            config_provider=config_provider,
            target_type="document",
            target_id=document_id,
        ) as (principals, audit):
            session, retrieval = service()
            try:
                result = retrieval.search_in_document(
                    document_id,
                    query,
                    principals=principals,
                    version_id=version_id,
                    limit=limit,
                    offset=offset,
                    chunks_per_page=chunks_per_page,
                )
                audit["found"] = result is not None
                if result is not None:
                    audit["results"] = len(result["results"])
                return result
            finally:
                session.close()

    @mcp.tool(
        title="Export the original file",
        tags={"read"},
        description=(
            "Download/export/copy one document into the current client workspace as the exact "
            "original Word, Excel, PDF, email, or other binary—never a text reconstruction. "
            "Call this whenever a task says download, save, copy, export, provide, or put a "
            "document in the workspace. Every result carries a short-lived download link "
            "and a curl command; a small original ALSO rides along as a base64 "
            "BlobResourceContents, so write it out from there rather than fetching it. "
            "inline_blob decides that explicitly: TRUE always attaches the bytes — pass it "
            "if you will write the file out and cannot reach this appliance over the "
            "network, which a sandboxed agent generally cannot — and FALSE never does. "
            "Left unset, the bytes come only when they are small enough to be worth "
            "carrying; above that you get the link, because a base64 original is not "
            "something you can read and costs you the context to hold. The result includes "
            "SHA-256, size, MIME type, filename, and exact citations."
        )
    )
    def download_document(
        document_id: str,
        version_id: str | None = None,
        source_object_id: str | None = None,
        inline_blob: bool | None = None,
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
                # Who carries the bytes. A client that will WRITE THE FILE OUT and
                # cannot reach the appliance — an agent in a sandbox, which is what
                # made the link alone insufficient — passes inline_blob=true and
                # always gets them. A client that will fetch the link passes false.
                # A caller that says nothing is usually a model, and for a model the
                # blob is context spent on a base64 zip it cannot read, so it gets
                # them only while they are small enough not to matter.
                inline_blob = (
                    downloadable.size_bytes <= _INLINE_BLOB_AUTO_MAX_BYTES
                    if inline_blob is None
                    else inline_blob
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
                            f"Exact original: {downloadable.filename} "
                            f"({downloadable.size_bytes} bytes, SHA-256 "
                            f"{downloadable.content_hash}). "
                            + (
                                "The file itself is attached to this result as a "
                                "base64 blob — write it out from there. "
                                if inline_blob
                                else "The bytes are not attached: this original is "
                                "larger than is worth carrying in a result. Fetch it "
                                "with the command below, or call again with "
                                "inline_blob=true if you cannot reach this appliance "
                                "and will write the file out yourself. "
                            )
                            + "If your client can reach this appliance directly, "
                            f"this also works:\n{save_command}"
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
            "context and graph-ready edges; rows and edges carry document identity and the "
            "provenance that established each relation - get_document on an endpoint "
            "returns its citation record. "
            "Shared-matter context is not represented as an inferred legal reference. "
            "PAGINATION: related documents arrive in related_documents (stored relations "
            "first, then shared-matter/thread context) with a page block alongside — "
            "{offset, limit, returned, has_more, next_offset, total}. total is the exact "
            "number of related documents you may see; edges and explicit_edges cover the "
            "documents on THIS page only. When page.has_more is true, call again with "
            "offset=page.next_offset before concluding the graph is complete."
        )
    )
    def find_related_documents(
        document_id: str,
        include_same_matter: bool = True,
        limit: int = 50,
        offset: int = 0,
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
            limit, offset = _page_bounds(limit, offset, max_limit=250)
            session, retrieval = service()
            try:
                result = retrieval.find_related_documents(
                    document_id,
                    principals=principals,
                    include_same_matter=include_same_matter,
                    limit=limit,
                    offset=offset,
                )
                audit.update(
                    found=result is not None,
                    result_count=result["result_count"] if result else 0,
                    has_more=bool(result and result["page"]["has_more"]),
                    offset=offset,
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
            "includes clearly labeled shared-matter/thread context. Each edge carries its "
            "endpoints and provenance; get_document on an endpoint returns its citation "
            "record. Only edges whose BOTH endpoints "
            "you may see are returned, and the limit counts those — an edge hidden by access "
            "control does not consume a slot in your page."
            + _PAGINATION_CONTRACT
        )
    )
    def traverse(
        entity_type: str,
        entity_id: str,
        limit: int = 100,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.traverse",
            headers,
            config_provider=config_provider,
            target_type=entity_type,
            target_id=entity_id,
        ) as (principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=250)
            session, retrieval = service()
            try:
                page = retrieval.traverse_page(
                    entity_type,
                    entity_id,
                    principals=principals,
                    limit=limit,
                    offset=offset,
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                )
                return _paged(page)
            finally:
                session.close()

    @mcp.tool(
        title="Matters the caller can read",
        tags={"read"},
        description=(
            "List matters that contain at least one version visible to the caller. Each row "
            "carries the matter's identity (title, reference numbers, practice area, kind) "
            "and visible_versions — the COUNT of document versions you could cite. The "
            "`firm_team` lists this firm's own people on the matter — name, role "
            "('Responsible Partner', 'Billing Partner', 'Lead Associate'), title and "
            "their practice group — so 'who ran this' and 'what has this partner "
            "done' are answered from the row. firm_person=... filters to a lawyer's "
            "matters by name or surname. These are FIRM people, never clients or "
            "opposing counsel; counterparties are in `parties` on document rows. "
            "`practice_group` is the FIRM'S OWN practice book, named as that firm names its "
            "groups in its own paperwork — the group that OWNS the matter, being the group of the "
            "partner responsible for it. `practice_groups` lists EVERY group with "
            "someone on the matter and `supporting_groups` lists the ones seconded "
            "in, owner excluded. The practice_group=... filter matches ANY of them, "
            "so a matter staffed jointly by two groups is returned "
            "to either — and on a filtered page every row carries `group_match`: "
            "'owner' when the group you asked for runs the matter, 'supporting' when "
            "it was merely staffed onto one another group runs. MIND THAT "
            "DIFFERENCE. 'Which matters does this group have' means the ones it "
            "owns; a specialist seconded onto another group's deal is work this group has "
            "TOUCHED, and counting it inflates the answer. Say which you are "
            "reporting, or filter the page down to group_match='owner'. "
            "An EMPTY `practice_group` means ownership was never RECORDED — not that "
            "no group owns the matter, and not that another one does. On such a row "
            "'supporting' only says 'not recorded as owner', so do not count the "
            "matter out of a group's book on it: open the matter's documents and "
            "decide ownership from what they say. "
            "Names are resolved, not string-matched: a group's name with 'and' for '&', with "
            "or without a trailing 'Group' or 'Department', and its other recorded "
            "spellings all reach the same book, and firm_person=... takes a surname, a full name, "
            "'Surname, First' or an initial. WHEN A QUESTION NAMES A PRACTICE "
            "('our banking matters', 'our funds'), SCOPE WITH "
            "practice_group, NOT practice_area: the group is how the firm organises "
            "itself, the area is what body of law applies, and they differ. A matter "
            "about corporate entity formation can be filed in the funds practice. "
            "The row also states how the firm FILED the matter and whether it happened: `instrument` "
            "(revolving credit facility, term loan B, senior notes offering, chapter 11, "
            "fund formation), `principal_document` (the file that constitutes the matter), "
            "`summary`, and `lifecycle` — executed | closed | terminated | dormant | "
            "in_progress | prospective. `prospective` means the firm is engaged and working "
            "but the instrument that would constitute the matter does not exist yet in "
            "any form, not even a draft — a mandate rather than a deal. `in_progress` "
            "means it exists and is being negotiated or closed. `executed` records that "
            "signed papers exist — the deal happened. It says NOTHING about whether the "
            "engagement's work continues: most executed matters are finished files, some "
            "are still live (a closing pending, post-closing work running), and the value "
            "is the same for both. `closed` marks finished work only where no instrument "
            "was ever signed. So no lifecycle value separates 'file finished' from 'file "
            "still active' on a deal matter: when a question turns on whether or WHEN "
            "work ended, the file's own closing papers — a closing letter, a final "
            "invoice, a disengagement letter — decide it, and finding those is how "
            "completed matters are identified. USE THESE TO BUILD THE "
            "CANDIDATE SET AND CHOOSE WHAT TO READ FIRST — NOT TO SETTLE MEMBERSHIP. "
            "`instrument`, `matter_kind`, `practice_area` and the rest are the firm's "
            "FILING LABELS: someone recorded them once, they can be stale, coarse or "
            "plain wrong, and they are never themselves evidence. When a label says a "
            "matter qualifies, confirm it in the matter's principal document before "
            "asserting it; when a label says it does not, that alone is no grounds to "
            "drop the matter — dropping on a label without opening anything is how "
            "right answers go missing. A filing label is one honest description, not "
            "an identity: a matter can be filed under one heading while its operative "
            "documents constitute something a given question would count differently. "
            "When membership in an answer turns on what a matter is, documents outrank "
            "the filing label, and the label outranks a stray mention. "
            "A matter qualifies on what it IS, not on what its documents "
            "mention: a term loan that refinances or repays a revolving facility is a term "
            "loan, and the facility it repays belongs to someone else's deal. When a question is "
            "about live or completed work, terminated, dormant and prospective matters are "
            "the ones where nothing was produced — whether they belong in a given answer "
            "is yours to judge from the question. lifecycle=... filters to one state, so 'which "
            "financings actually closed' is a single call rather than reading every "
            "document of every candidate. But lifecycle says where the PAPERS stand, not "
            "whether the deal exists and not whether the work ended: an in_progress facility is still a "
            "facility this firm is papering, with a credit agreement and security "
            "documents in the folder. Narrow to lifecycle='executed' only when the "
            "question turns on the closing itself ('which deals closed', 'signed in "
            "2025'). For 'which matters involve X' or 'how many have we done', "
            "in_progress belongs in the set — excluding it drops live matters and "
            "reports a count that is too low, which reads as a confident answer. "
            "THIS IS A CANDIDATE SET, NOT AN ANSWER. A group's book is every matter "
            "the group works; the answer to a question about that book is almost always "
            "a subset of it, decided by a test the listing cannot apply — secured, "
            "revolving, covenanted, underwritten. So the number of rows here is NOT the "
            "number a question asks for, and `has_more: false` means you have the whole "
            "book, never that you have the answer. Verify every candidate against the "
            "question's own test by reading a document, then report the set that passes. "
            "Reporting the size of the book as the answer to a qualifying question is the "
            "characteristic way to be confidently wrong here. "
            "include_documents=true adds `source_paths` to every row: the full path of "
            "every file in that matter, complete, nothing else. It is how you see what a "
            "whole practice actually holds in ONE call instead of opening each matter in "
            "turn — and opening each matter in turn is where matters get dropped, because "
            "a matter judged from search hits and never opened is a matter whose deciding "
            "document you never saw. Use it when a question spans a practice — that is a "
            "couple of hundred kilobytes and costs no measurable time — and leave it off "
            "when you already know the matter or are browsing the whole estate, where the "
            "paths of every matter at once are megabytes you will not read. "
            "citations themselves are not in the listing: open the matter with search_filter "
            "(or a document with get_document) to get them. "
            "practice_area filters by an Area of Law ontology node id with SUBTREE semantics "
            "(a parent area matches its children); matter_kind does the same over the "
            "Service facet — what the firm is DOING, which is a different question from "
            "which law applies, and the two compose. Both ids come from ontology_search, "
            "whose every result names the filter it belongs in. "
            "A matter's practice area is a property of the whole matter, so a matter can "
            "sit in one area while holding documents that read like another. "
            "Ordered by matter title; the limit counts matters you can actually see, so a "
            "full page means a full page. A firm typically has hundreds to thousands of "
            "matters: this tool is for browsing them, and finding one specific matter is "
            "faster with search_semantic or a search_filter on its identifier. No total is "
            "reported here — counting visible matters costs the same as fetching every page "
            "— so use page.has_more, and never answer 'how many matters' from one page."
            + _PAGINATION_CONTRACT
        )
    )
    def list_matters(
        limit: int = 100,
        offset: int = 0,
        practice_area: str | None = None,
        matter_kind: str | None = None,
        lifecycle: str | None = None,
        practice_group: str | None = None,
        firm_person: str | None = None,
        include_documents: bool = False,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory, "mcp.list_matters", headers, config_provider=config_provider
        ) as (principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=250)
            session, retrieval = service()
            try:
                page = retrieval.list_matters_page(
                    principals=principals,
                    limit=limit,
                    offset=offset,
                    practice_area=practice_area,
                    matter_kind=matter_kind,
                    lifecycle=lifecycle,
                    practice_group=practice_group,
                    firm_person=firm_person,
                    include_documents=include_documents,
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                    practice_area=practice_area,
                )
                return _paged(page)
            finally:
                session.close()

    @mcp.tool(
        title="Everything in one matter",
        tags={"read"},
        description=(
            "The COMPLETE file listing of one matter — every document you can see, in "
            "folder order, with its source_path, title, document type, date and version "
            "status. This is the matter's folder as a partner opening it would see it, "
            "and it is the right first call once you know which matter a question is "
            "about. NOT PAGINATED: what comes back is all of it, so absence here is "
            "real absence and you may say 'this matter contains no X' on the strength "
            "of it. That is exactly what you may NOT conclude from a page of "
            "search_filter, which shows a window of a matter that may hold a hundred "
            "documents — reading twenty rows and concluding the rest are not there is "
            "how an answer comes to say a matter has no partnership agreement while its "
            "executed partnership agreement sits in the folder. Rows carry no text: "
            "this says what the matter CONTAINS, get_document says what a document SAYS."
            + _COMPLETE_RESULT
        ),
    )
    def list_matter_documents(
        matter_id: str, headers: dict[str, str] = CurrentHeaders()
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.list_matter_documents",
            headers,
            config_provider=config_provider,
        ) as (principals, audit):
            session, retrieval = service()
            try:
                results = retrieval.list_matter_documents(
                    matter_id, principals=principals
                )
                audit.update(result_count=len(results), matter_id=matter_id)
                return {"results": results, "complete": True, "count": len(results)}
            finally:
                session.close()

    @mcp.tool(
        title="This firm's own lawyers and their practice groups",
        tags={"read"},
        description=(
            "The firm's internal directory: the lawyers who staff matters, each with "
            "their title (Partner, Counsel, Associate), the practice group they sit in, "
            "the roles they hold ('Responsible Partner', 'Billing Partner', 'Lead "
            "Associate') and matter_count — the COUNT of matters visible to you that "
            "they work. These are THIS FIRM'S people. Clients, counterparties and "
            "opposing counsel are not here; resolve those with resolve_entity. "
            "Use this to find out what a practice group is called and who is in it "
            "before scoping with list_matters(practice_group=...), and to get a "
            "lawyer's name right before list_matters(firm_person=...) — both filters "
            "match on the name as the firm writes it. "
            "practice_group=... narrows the directory to one group, name=... matches a "
            "full name or a surname. Someone appears only if you can see a matter they "
            "are on, and matter_count counts only those matters. "
            "The matters themselves are not listed here: open them with "
            "list_matters(firm_person=...), which pages and carries each matter's "
            "lifecycle, instrument and summary. `total` is exact."
            + _PAGINATION_CONTRACT
        )
    )
    def list_firm_people(
        limit: int = 100,
        offset: int = 0,
        practice_group: str | None = None,
        name: str | None = None,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.list_firm_people",
            headers,
            config_provider=config_provider,
        ) as (principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=250)
            session, retrieval = service()
            try:
                page = retrieval.list_firm_people(
                    principals=principals,
                    limit=limit,
                    offset=offset,
                    practice_group=practice_group,
                    name=name,
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                    practice_group=practice_group,
                )
                return _paged(page)
            finally:
                session.close()

    @mcp.tool(
        title="Invoiced total and fees per task code",
        tags={"read"},
        description=(
            "Roll up a matter's billing: invoiced total plus hours and fees per UTBMS task "
            "code. Restricted to matters the caller can see and fails closed if any invoice "
            "lacks an exact project/document/source-object citation. Covers every invoice on "
            "the matter in one call — the totals are complete, not a page."
            + _COMPLETE_RESULT
        )
    )
    def billing_rollup(matter_id: str, headers: dict[str, str] = CurrentHeaders()) -> dict:
        with audited_call(
            session_factory, "mcp.billing_rollup", headers, config_provider=config_provider
        ) as (principals, audit):
            from knowledge_index.pipeline.billing import billing_rollup as _rollup

            session, retrieval = service()
            try:
                if not retrieval.matter_visible(matter_id, principals):
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
            "can see; each row carries the ids of the backing documents (document_ids) - "
            "get_document returns their citation records. Ordered by invoice date, oldest "
            "first, with page.total giving the "
            "matter's exact invoice count. For an invoiced total across the whole matter "
            "call billing_rollup instead of summing pages by hand."
            + _PAGINATION_CONTRACT
        )
    )
    def list_invoices(
        matter_id: str,
        limit: int = 100,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory, "mcp.list_invoices", headers, config_provider=config_provider
        ) as (principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=250)
            session, retrieval = service()
            try:
                if not retrieval.matter_visible(matter_id, principals):
                    audit["result_count"] = 0
                    return _paged(Page(items=[], offset=offset, limit=limit, total=0))
                total = (
                    session.scalar(
                        select(func.count())
                        .select_from(BillingInvoice)
                        .where(BillingInvoice.matter_id == matter_id)
                    )
                    or 0
                )
                invoices = session.scalars(
                    select(BillingInvoice)
                    .where(BillingInvoice.matter_id == matter_id)
                    # Total order, so the same offset means the same invoice on
                    # every call; invoice_date alone repeats within a billing run.
                    .order_by(
                        BillingInvoice.invoice_date.nullslast(), BillingInvoice.id
                    )
                    .offset(offset)
                    .limit(limit)
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
                            # Collection row: the invoice's identity plus the ids
                            # of the documents that back it — the citation
                            # records themselves come from get_document.
                            "document_ids": sorted(
                                {
                                    citation["document"]["id"]
                                    for citation in citations
                                    if citation.get("document")
                                }
                            ),
                        }
                    )
                page = Page(
                    items=result,
                    offset=offset,
                    limit=limit,
                    has_more=offset + len(result) < total,
                    total=total,
                )
                audit.update(
                    result_count=len(result), has_more=page.has_more, offset=offset
                )
                return _paged(page)
            finally:
                session.close()

    @mcp.tool(
        title="Resolve a party or client name",
        tags={"read"},
        description=(
            "Resolve a party or client name or identifier (LEI, HRB, VAT) to the firm's known "
            "entities, including entities that share an identifier. Results without an exact "
            "authorized project/document/source-object citation are withheld, so a page can "
            "be shorter than the limit while more matches remain — read page.has_more rather "
            "than the row count. Different companies do share names: several results is a "
            "real answer, not a failure to disambiguate, so page rather than assume the first "
            "hit is the one meant."
            + _PAGINATION_CONTRACT
        )
    )
    def resolve_entity(
        query: str,
        limit: int = 20,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory, "mcp.resolve_entity", headers, config_provider=config_provider
        ) as (principals, audit):
            from knowledge_index.pipeline.billing import resolve_entity as _resolve

            limit, offset = _page_bounds(limit, offset, max_limit=100)
            session, retrieval = service()
            try:
                # Ask the resolver for the whole window plus the probe row, then
                # drop entities the caller holds no citation for. Filtering after
                # the fetch is why `has_more` here comes from the probe and not
                # from a count: the count would include withheld entities.
                raw = _resolve(session, query, limit=offset + limit + 1)
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
                            # Collection row: a frequently-cited entity can be
                            # backed by hundreds of documents, so the row
                            # advertises the COUNT; drill into the documents
                            # with search_semantic/search_filter on the entity.
                            "citation_count": len(citations),
                        }
                    )
                # `result` starts at the first authorized entity, not at `offset`,
                # so the window is cut here rather than by Page.probe (which
                # expects an already-offset list). No total: the resolver was
                # asked only as deep as this window, so len(result) is a bound on
                # the matches, not a count of them.
                window = result[offset : offset + limit]
                page = Page(
                    items=window,
                    offset=offset,
                    limit=limit,
                    has_more=len(result) > offset + len(window),
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                    **_query_fingerprint(query),
                )
                return _paged(page)
            finally:
                session.close()

    @mcp.tool(
        title="Search drafting and negotiation rationale",
        tags={"read"},
        description=(
            "Search anonymized drafting and negotiation rationale. Underlying evidence remains "
            "protected by the source document ACL. Every result includes the exact evidence "
            "project, document version, and source object citation. Ranked by lexical match, "
            "best first; page.total is the exact number of authorized records that matched, "
            "so it is a sound basis for 'how often has the firm reasoned about X'."
            + _PAGINATION_CONTRACT
        )
    )
    def search_decisions(
        query: str,
        limit: int = 20,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.search_decisions",
            headers,
            config_provider=config_provider,
        ) as (
            principals,
            audit,
        ):
            limit, offset = _page_bounds(limit, offset, max_limit=100)
            session, retrieval = service()
            try:
                page = retrieval.search_decisions_page(
                    query, principals=principals, limit=limit, offset=offset
                )
                audit.update(
                    result_count=len(page.items),
                    has_more=page.has_more,
                    offset=offset,
                    **_query_fingerprint(query),
                )
                return _paged(page)
            finally:
                session.close()

    @mcp.tool(
        title="Document, task and practice-area vocabularies",
        tags={"read"},
        description=(
            "The active document-type ontology and the stable auxiliary taxonomies. "
            "Document types are a hierarchy, not a flat list: browse it with "
            "ontology_children or find a node with ontology_search, then pass the "
            "node id as the doc_type filter (it matches the node's whole subtree). "
            "Returns the ontology's ROOTS and the top two levels of practice areas, "
            "not every node — ontology.visible_nodes is the true node count, and the "
            "deeper levels are reached with ontology_children/ontology_search."
            + _COMPLETE_RESULT
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
        title="Search every ontology facet",
        tags={"read"},
        description=(
            "Find ontology nodes by name, synonym or definition across EVERY active "
            "facet at once: document types, areas of law, services (what the firm is "
            "DOING) and contractual clauses. This is where a filter value comes from. "
            "Each result carries `facet` and `use_as_filter` — the name of the parameter "
            "its id belongs in: doc_type, practice_area, matter_kind or clause_type. Pass "
            "an id to the parameter the node names and no other; the ids look alike, and "
            "one given to the wrong parameter matches nothing. facet='area_of_law' (or "
            "doc_type / service / clause) narrows the search to one facet when you know "
            "which you want. Every filter matches the node AND everything below it, so an "
            "interior node like 'Agreements' covers every agreement type. Ranked "
            "best-match first (exact label, then prefix, then substring, then synonym, "
            "then definition), and page.total is the exact number of nodes that matched."
            + _PAGINATION_CONTRACT
        ),
    )
    def ontology_search(
        query: str,
        facet: str | None = None,
        limit: int = 12,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory, "mcp.ontology_search", headers, config_provider=config_provider
        ) as (_principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=100)
            # The ontology is an in-memory artifact, so asking for the whole
            # match set costs no more than asking for a page of it — which is
            # what makes an exact total free here.
            scope = config_provider().browse_ontology()
            results = [_with_filter(node) for node in scope.search(query, limit=None)]
            if facet:
                results = [node for node in results if node.get("facet") == facet]
            page = Page.slice(results, offset=offset, limit=limit)
            audit.update(
                result_count=len(page.items),
                has_more=page.has_more,
                offset=offset,
                query=query,
            )
            return _paged(page)

    @mcp.tool(
        title="Ontology top-level branches",
        tags={"read"},
        description=(
            "Top-level branches of every active facet — document types, areas of law, "
            "services, clauses — in one call. Each root carries `facet` and "
            "`use_as_filter`, the parameter that takes ids from its subtree. Descend with "
            "ontology_children."
            + _COMPLETE_RESULT
        ),
    )
    def ontology_roots(headers: dict[str, str] = CurrentHeaders()) -> dict:
        with audited_call(
            session_factory, "mcp.ontology_roots", headers, config_provider=config_provider
        ) as (_principals, audit):
            results = [
                _with_filter(node)
                for node in config_provider().browse_ontology().roots()
            ]
            audit["result_count"] = len(results)
            return {"results": results, "complete": True}

    @mcp.tool(
        title="Descend the ontology one level",
        tags={"read"},
        description=(
            "Children of one ontology node, with definitions — descend any facet's "
            "hierarchy one level at a time. Each child carries `facet` and "
            "`use_as_filter`. A child count of 0 marks a leaf. page.total is "
            "the node's exact number of visible children, so a node whose children you have "
            "not fully paged is a subtree you have not fully seen."
            + _PAGINATION_CONTRACT
        ),
    )
    def ontology_children(
        node_id: str,
        limit: int = 100,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory, "mcp.ontology_children", headers, config_provider=config_provider
        ) as (_principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=250)
            results = [
                _with_filter(node)
                for node in config_provider().browse_ontology().children(node_id)
            ]
            page = Page.slice(results, offset=offset, limit=limit)
            audit.update(
                result_count=len(page.items),
                has_more=page.has_more,
                offset=offset,
                node_id=node_id,
            )
            return _paged(page)

    @mcp.tool(
        title="Read one ontology node",
        tags={"read"},
        description=(
            "Full detail for one node of any facet: definition, synonyms, path, parents, "
            "plus `facet` and `use_as_filter` — the filter parameter its id belongs in."
        ),
    )
    def ontology_node(node_id: str, headers: dict[str, str] = CurrentHeaders()) -> dict:
        with audited_call(
            session_factory, "mcp.ontology_node", headers, config_provider=config_provider
        ) as (_principals, audit):
            detail = config_provider().browse_ontology().node(node_id)
            if detail:
                detail = _with_filter(detail)
            audit["node_id"] = node_id
            return detail or {"error": f"unknown or inactive node {node_id!r}"}

    @mcp.tool(
        title="What the caller may see, before ranking",
        tags={"scope"},
        description=(
            "Compile the caller's ACL and optional project/document selections into the exact "
            "scope that will constrain hybrid/vector retrieval before scoring. "
            "project_count and document_count are ALWAYS the exact size of the compiled "
            "scope — they are the answer to 'how much can I see'. The enumerated "
            "document_ids and their citations are paginated because a scope routinely "
            "covers tens of thousands of documents: documents = {results, page} with the "
            "same page contract as the other tools, and the projects list is complete. "
            "Do not infer scope size from len(documents.results); read document_count."
        ),
    )
    def preview_search_scope(
        project_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        headers: dict[str, str] = CurrentHeaders(),
    ) -> dict:
        with audited_call(
            session_factory,
            "mcp.preview_search_scope",
            headers,
            config_provider=config_provider,
        ) as (principals, audit):
            limit, offset = _page_bounds(limit, offset, max_limit=500)
            session = session_factory()
            try:
                scope = AccessService(session).compile_scope(
                    principals,
                    project_ids=project_ids or [],
                    document_ids=document_ids or [],
                )
                retrieval = RetrievalService(session, config_provider())
                # Citations are built only for the documents on this page. The
                # unpaginated form fetched every citation in the caller's whole
                # scope to answer a question about its size, which the exact
                # counts below already answer.
                in_scope = list(scope.document_ids)
                documents = Page.slice(in_scope, offset=offset, limit=limit)
                result = {
                    "fingerprint": scope.fingerprint,
                    "project_count": len(scope.project_ids),
                    "document_count": len(scope.document_ids),
                    "project_ids": list(scope.project_ids),
                    "projects": [
                        project
                        for project_id in scope.project_ids
                        if (project := retrieval.project_reference(project_id)) is not None
                    ],
                    # The scope preview is a listing: ids and counts. Citation
                    # records for any of these documents come from get_document.
                    "documents": _paged(documents),
                }
                audit.update(
                    fingerprint=scope.fingerprint,
                    project_count=len(scope.project_ids),
                    document_count=len(scope.document_ids),
                    offset=offset,
                    has_more=documents.has_more,
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


def _empty_result_help(
    page: Page,
    retrieval,
    principals: set[str],
    filters: SearchFilters,
) -> dict:
    """Extra envelope keys that turn an empty filtered result into an actionable one.

    An empty list does not distinguish a misspelled filter from an empty matter
    from something the caller may not see, so a client either guesses again or
    abandons the matter — both measured on real agent traffic, where 41 filter
    calls returned nothing and the caller almost never recovered. When filters were
    set and nothing matched, this returns the values that WOULD have matched in
    this caller's own scope.

    It lands in its own ``no_results`` key rather than inside ``results``. It used
    to be smuggled in as a lone advisory row with no ``citations``, relying on the
    server instructions to stop a caller treating it as a hit; a sibling key
    cannot be miscounted as a result at all — which also keeps ``page.returned``
    honest, since an advisory is not a row.

    Only ever consulted for the first page: an empty page at a non-zero offset
    means the caller paged past the end, which the filters already explained.
    """
    # The matter-level filters are here for the same reason as the rest: they are
    # the ones a caller is most likely to get subtly wrong. A practice_area takes
    # an Area-of-Law node id, and the ontology_* tools navigate DOCUMENT TYPES —
    # so a caller can resolve a plausible-looking id there, pass it here, and get
    # a clean empty page that means "that is not an area of law", not "this firm
    # has no such matters".
    _NAMES = (
        "party", "identifier", "doc_type", "matter_id",
        "practice_area", "practice_group", "firm_person", "lifecycle",
    )
    if page.items or page.offset or not any(
        getattr(filters, name, None) for name in _NAMES
    ):
        return {}
    if filters.practice_area:
        try:
            area_scope = retrieval.config.ontology_facet("area_of_law")
        except Exception:  # noqa: BLE001 - no facet, no advice to give
            area_scope = None
        if area_scope is not None and filters.practice_area not in area_scope.visible:
            return {
                "no_results": {
                    "filters_applied": ["practice_area"],
                    "message": (
                        f"{filters.practice_area!r} is not an Area of Law node, so "
                        "nothing could match it — most likely it belongs to another "
                        "facet. Call ontology_node on it: every node reports its "
                        "`facet` and the `use_as_filter` parameter its id belongs "
                        "in. ontology_search(facet='area_of_law') finds the right "
                        "one, or scope by the firm's own book with "
                        "practice_group, which takes the firm's own name for a group rather than a node id."
                    ),
                }
            }
    try:
        suggestions = retrieval.suggest_for_empty(principals=principals, filters=filters)
    except Exception:  # never turn a clean empty result into an error
        return {}
    applied = sorted(name for name in _NAMES if getattr(filters, name, None))
    advisory = {
        "filters_applied": applied,
        "message": (
            "No documents matched those filters in your access scope. These values exist "
            "here — reissue the call with one of them, or drop the filter."
            if suggestions
            else (
                "No documents matched those filters in your access scope, and nothing "
                f"resembling the {'/'.join(applied)} you gave is indexed. Drop that filter "
                "and search by content instead — a company or project name is usually a "
                "party or a search term, not a legal identifier."
            )
        ),
    }
    if suggestions:
        advisory["did_you_mean"] = suggestions
    return {"no_results": advisory}


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
