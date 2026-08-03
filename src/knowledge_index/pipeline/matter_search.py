"""Ingestion-time matter search: the tools the classification agent uses to link a
document to one of the firm's existing matters.

A firm has hundreds to thousands of matters, so the old "dump the first 80 matters
into the prompt" approach silently drops most of them. Instead the agent SEARCHES:
semantically over already-indexed chunks, lexically over matter titles, and by exact
reference number, then reads the folder neighbourhood the way a paralegal would. All
of this is unscoped by design — classification legitimately needs corpus-wide
visibility to find the right matter — and never runs on the user query path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Client,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    EntityIdentifier,
    Matter,
    MatterAssignment,
    Party,
    Project,
    SourceObject,
)
from knowledge_index.pipeline.folder_context import (
    _parent,
    folder_ls,
    list_one_folder,
    revisions_digest,
)
from knowledge_index.pipeline.providers import AgentTool, embed_text


def search_documents(session: Session, config: AppConfig, query: str, *, limit: int = 10) -> list[dict]:
    """Find documents anywhere in the index by title or topic — used by the relation
    agent to locate a referenced master contract, judgment, or exhibit filed under a
    different folder or matter. Returns metadata only (no full text)."""
    query = (query or "").strip()
    if not query:
        return []
    scored: dict[str, float] = {}
    try:
        from knowledge_index.search_backend import OpenSearchIndex

        vector = embed_text(query, config)
        for rank, hit in enumerate(OpenSearchIndex(config).matter_hits_by_vector(vector, size=40)):
            document_id = (hit.get("_source") or {}).get("document_id")
            if document_id:
                scored[document_id] = max(scored.get(document_id, 0.0), 1.0 / (10 + rank))
    except Exception:
        pass
    for document in session.scalars(
        select(Document).where(Document.title.ilike(f"%{query}%")).limit(20)
    ):
        scored[document.id] = scored.get(document.id, 0.0) + 0.4

    results: list[dict] = []
    for document_id, _score in sorted(scored.items(), key=lambda item: item[1], reverse=True)[:limit]:
        document = session.get(Document, document_id)
        if document is None:
            continue
        matter = session.get(Matter, document.matter_id) if document.matter_id else None
        observations = session.execute(
            select(SourceObject.id, SourceObject.path)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObject.id,
            )
            .join(DocumentVersion, DocumentVersion.id == DocumentVersionSource.version_id)
            .where(DocumentVersion.document_id == document.id)
            .limit(8)
        ).all()
        results.append(
            {
                "document_id": document.id,
                "title": document.title,
                "doc_type": document.doc_type,
                "matter_ref": (matter.reference_numbers or [None])[0] if matter else None,
                "source_files": [
                    {"ref": source_object_id, "path": path}
                    for source_object_id, path in observations
                ],
            }
        )
    return results


def _structured_artifact(session: Session, content_hash: str | None) -> Artifact | None:
    if not content_hash:
        return None
    return session.scalar(
        select(Artifact)
        .where(
            Artifact.content_hash == content_hash,
            Artifact.kind == "structured_json",
        )
        .order_by(Artifact.created_at.desc())
    )


def open_source_file(
    session: Session,
    source_id: str,
    path: str,
    *,
    offset: int = 0,
    max_chars: int = 12000,
    ensure_ready: Callable[[str], dict] | None = None,
) -> dict:
    """Open one converted file by its exact listed path, with pageable text.

    ``ensure_ready`` (when given) pulls an unconverted file's fetch/convert forward
    through the normal pipeline claim machinery, so the relation agent can read
    neighbours that merely have not been scheduled yet.
    """
    source_object = session.scalar(
        select(SourceObject).where(
            SourceObject.source_id == source_id,
            SourceObject.path == path,
            SourceObject.deleted_at.is_(None),
        )
    )
    if source_object is None:
        return {"error": "file not found at that exact path", "path": path}
    artifact = _structured_artifact(session, source_object.content_hash)
    if artifact is None and ensure_ready is not None:
        outcome = ensure_ready(source_object.id)
        # the inline stages committed in their own sessions; drop cached state so the
        # re-read below sees the fresh content_hash and artifact
        session.expire_all()
        source_object = session.scalar(
            select(SourceObject).where(
                SourceObject.source_id == source_id,
                SourceObject.path == path,
                SourceObject.deleted_at.is_(None),
            )
        )
        if source_object is not None:
            artifact = _structured_artifact(session, source_object.content_hash)
        if source_object is None or artifact is None:
            return {
                "error": (
                    f"file is not readable yet ({outcome.get('status', 'unknown')}); "
                    "do not link it from this side — when it is processed, its own "
                    "relation pass looks back at the current file"
                ),
                "path": path,
            }
    if artifact is None:
        return {
            "error": "file has not finished conversion",
            "ref": source_object.id,
            "path": source_object.path,
        }
    payload = artifact.payload or {}
    text = str(payload.get("text") or "")
    offset = max(0, min(int(offset), len(text)))
    max_chars = max(1000, min(int(max_chars), 20000))
    result = {
        "ref": source_object.id,
        "path": source_object.path,
        "filename": source_object.name,
        "offset": offset,
        "returned_chars": min(max_chars, max(0, len(text) - offset)),
        "total_chars": len(text),
        "text": text[offset : offset + max_chars],
    }
    # the text above is the ACCEPTED view — a markup is only recognizable by its
    # tracked changes, so a compact digest rides along for the relation agent
    digest = revisions_digest(payload.get("revisions"), max_entries=10, max_chars=1500)
    if digest is not None:
        result["tracked_changes"] = digest
    return result


def relation_tools(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    source_id: str,
    primary_folder_path: str,
    *,
    opened_refs: set[str] | None = None,
    ensure_ready: Callable[[str], dict] | None = None,
) -> list[AgentTool]:
    """Tools for the relation agent, each using its own short database session.

    Relation inference can spend minutes waiting on model/tool API calls. Never bind those
    calls to the transaction that will later materialize the result.
    """

    def list_folder(args: dict) -> str:
        with session_factory() as session:
            requested = str(args.get("path") or "").strip()
            if requested:
                return list_one_folder(session, source_id, requested)
            return folder_ls(
                session,
                source_id,
                primary_folder_path,
                up=1,
                down=1,
                per_folder_limit=None,
                max_folders=None,
            )

    def find_documents(args: dict) -> str:
        with session_factory() as session:
            return json.dumps(
                search_documents(session, config, str(args.get("query", ""))),
                ensure_ascii=False,
            )

    def open_file(args: dict) -> str:
        with session_factory() as session:
            result = open_source_file(
                session,
                source_id,
                str(args.get("path", "")),
                offset=int(args.get("offset", 0) or 0),
                max_chars=int(args.get("max_chars", 12000) or 12000),
                ensure_ready=ensure_ready,
            )
            if opened_refs is not None and result.get("ref") and not result.get("error"):
                opened_refs.add(str(result["ref"]))
            return json.dumps(result, ensure_ascii=False)

    return [
        AgentTool(
            name="list_folder",
            description=(
                "Without a path: show the directory listing supplied with the current "
                "file again (its own folder, one parent level, one child level). With a "
                "path: list THAT folder's direct files and subfolders — use it to look "
                "inside sibling folders shown name-only (e.g. Drafts/, Correspondence/) "
                "and learn the exact paths open_file needs. '/' lists the source root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Exact folder path to list; omit for the "
                        "standard neighbourhood view.",
                    }
                },
            },
            handler=list_folder,
        ),
        AgentTool(
            name="search_documents",
            description=(
                "Find documents anywhere in the index by title or topic — use it to locate a "
                "referenced master contract, judgment, or exhibit that lives in another folder "
                "or matter."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=find_documents,
        ),
        AgentTool(
            name="open_file",
            description=(
                "Open a file from the supplied directory listing by its exact path. Returns "
                "the stable source ref required for relationships plus converted text. Use "
                "offset to continue reading a long file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 20000,
                    },
                },
                "required": ["path"],
            },
            handler=open_file,
        ),
    ]


def _matter_folders(session: Session, matter_id: str, *, limit: int = 6) -> list[str]:
    paths = session.scalars(
        select(SourceObject.path)
        .join(MatterAssignment, MatterAssignment.source_object_id == SourceObject.id)
        .where(MatterAssignment.matter_id == matter_id, SourceObject.deleted_at.is_(None))
    ).all()
    return sorted({_parent(path) for path in paths})[:limit]


def _matter_summary(session: Session, matter: Matter) -> dict:
    member_count = (
        session.scalar(
            select(func.count())
            .select_from(MatterAssignment)
            .where(MatterAssignment.matter_id == matter.id)
        )
        or 0
    )
    return {
        "id": matter.id,
        "matter_ref": (matter.reference_numbers or [None])[0],
        "reference_numbers": matter.reference_numbers or [],
        "title": matter.title,
        "practice_area": matter.practice_area,
        "member_documents": member_count,
        "folders": _matter_folders(session, matter.id),
    }


def search_matters(session: Session, config: AppConfig, query: str, *, limit: int = 8) -> list[dict]:
    """Rank existing matters against a free-text query (party, ref, title, topic)."""
    query = (query or "").strip()
    if not query:
        return []
    scored: dict[str, float] = {}

    # Semantic: nearest already-indexed chunks -> their matters (skips cleanly when the
    # index is cold, i.e. nothing indexed yet on a fresh estate).
    try:
        from knowledge_index.search_backend import OpenSearchIndex

        vector = embed_text(query, config)
        for rank, hit in enumerate(OpenSearchIndex(config).matter_hits_by_vector(vector, size=40)):
            matter_id = (hit.get("_source") or {}).get("matter_id")
            if matter_id:
                scored[matter_id] = max(scored.get(matter_id, 0.0), 1.0 / (10 + rank))
    except Exception:
        pass

    # Lexical: matter title contains the query.
    for matter in session.scalars(
        select(Matter).where(Matter.title.ilike(f"%{query}%")).limit(30)
    ):
        scored[matter.id] = scored.get(matter.id, 0.0) + 0.5

    # Exact-ish: reference number substring over a bounded scan.
    needle = query.upper()
    for matter in session.scalars(select(Matter).limit(2000)):
        if any(needle in (ref or "").upper() for ref in (matter.reference_numbers or [])):
            scored[matter.id] = scored.get(matter.id, 0.0) + 1.0

    ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)[:limit]
    results: list[dict] = []
    for matter_id, score in ranked:
        matter = session.get(Matter, matter_id)
        if matter is None:
            continue
        summary = _matter_summary(session, matter)
        summary["match_score"] = round(score, 4)
        results.append(summary)
    return results


def peek_matter(session: Session, matter_id: str) -> dict:
    matter = session.get(Matter, matter_id) if matter_id else None
    if matter is None:
        return {"error": "matter not found"}
    summary = _matter_summary(session, matter)
    titles = session.scalars(
        select(Document.title).where(Document.matter_id == matter.id).limit(12)
    ).all()
    summary["document_titles"] = [title for title in titles if title]
    return summary


def _advisory_xact_lock(session: Session, key: str) -> None:
    """Serialize one logical entity on Postgres for the current transaction only."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))


def get_or_create_matter(
    session_factory: sessionmaker[Session],
    *,
    project_id: str | None,
    reference_number: str,
    title: str,
    provenance: dict | None = None,
    status: str = "unknown",
) -> dict:
    """Get-or-create a matter by (project, reference number), committed immediately.

    Documents of one matter classify in parallel, so the matter must become visible
    to the other workers the moment this returns — not when the calling stage's
    transaction commits minutes later. Hence the own short session and the commit
    here; the advisory lock makes the check-then-insert atomic per reference.
    """
    reference = (reference_number or "").strip().upper()
    if not reference:
        return {"error": "reference_number must not be empty"}
    if not (title or "").strip():
        return {"error": "title must not be empty"}
    with session_factory() as session:
        _advisory_xact_lock(session, f"matter-ref:{project_id or 'none'}:{reference}")
        existing = next(
            (
                item
                for item in session.scalars(select(Matter)).all()
                if reference in (item.reference_numbers or [])
                and (not project_id or item.project_id == project_id)
            ),
            None,
        )
        if existing is not None:
            summary = _matter_summary(session, existing)
            summary["created"] = False
            summary["note"] = "a matter with this reference already exists — use it"
            return summary
        if project_id and session.get(Project, project_id) is None:
            return {"error": "the source's assigned project is missing"}
        matter = Matter(
            project_id=project_id,
            reference_numbers=[reference],
            title=title.strip(),
            status=status,
            imported=False,
            provenance=provenance,
        )
        session.add(matter)
        session.flush()
        summary = _matter_summary(session, matter)
        summary["created"] = True
        session.commit()
        return summary


def classification_tools(
    session: Session,
    config: AppConfig,
    source_id: str,
    locus_path: str,
    *,
    session_factory: sessionmaker[Session] | None = None,
    project_id: str | None = None,
    fallback_reference: str | None = None,
    provenance: dict | None = None,
    seen_matter_ids: set[str] | None = None,
) -> list[AgentTool]:
    """Tools for the matter-classification agent, bound to one document's context.

    ``seen_matter_ids`` collects every matter id the agent saw in a tool result, so the
    caller can enforce that a submitted matter_id was actually looked at, not invented.
    """
    seen = seen_matter_ids if seen_matter_ids is not None else set()

    def run_search(args: dict) -> str:
        results = search_matters(session, config, str(args.get("query", "")))
        seen.update(result["id"] for result in results)
        return json.dumps(results, ensure_ascii=False)

    def run_peek(args: dict) -> str:
        result = peek_matter(session, str(args.get("matter_id", "")))
        if "id" in result:
            seen.add(result["id"])
        return json.dumps(result, ensure_ascii=False)

    def run_create(args: dict) -> str:
        assert session_factory is not None
        requested_reference = str(args.get("reference_number") or "").strip()
        result = get_or_create_matter(
            session_factory,
            project_id=project_id,
            reference_number=requested_reference or (fallback_reference or ""),
            title=str(args.get("title", "")),
            provenance=provenance,
            # a matter created under the folder-derived placeholder is a triage
            # pile, not a real case file — mark it so the UI can surface it
            status="unknown" if requested_reference else "unassigned",
        )
        if "id" in result:
            seen.add(result["id"])
        return json.dumps(result, ensure_ascii=False)

    tools = [
        AgentTool(
            name="search_matters",
            description=(
                "Search the firm's existing matters by any text: a party name, a reference "
                "number (Aktenzeichen), a matter title, or a topic. Returns candidate matters "
                "with their reference numbers, titles, practice area and folders."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Party, reference number, title, or topic to search for.",
                    }
                },
                "required": ["query"],
            },
            handler=run_search,
        ),
        AgentTool(
            name="list_folder",
            description=(
                "Show the folder neighbourhood around THIS document: its own folder in full, "
                "plus two levels up and down, with the files in each. Use it to see where the "
                "document is filed and which matter its neighbours belong to."
            ),
            parameters={"type": "object", "properties": {}},
            handler=lambda _args: folder_ls(session, source_id, locus_path),
        ),
        AgentTool(
            name="peek_matter",
            description=(
                "Show one matter in detail by its id (from a search_matters result): title, "
                "reference numbers, practice area, folders and a sample of its document titles."
            ),
            parameters={
                "type": "object",
                "properties": {"matter_id": {"type": "string"}},
                "required": ["matter_id"],
            },
            handler=run_peek,
        ),
    ]
    if session_factory is not None:
        tools.append(
            AgentTool(
                name="create_matter",
                description=(
                    "Create the matter for this document, or get it if the reference number "
                    "already exists (created=false in the result — assign to that matter "
                    "instead). Other documents of the same matter may be classifying in "
                    "parallel and only see this matter once it is created, so call this "
                    "IMMEDIATELY after your searches came up empty — never search, then read "
                    "around, then create. Omit reference_number only when the document and "
                    "path truly show none; a stable placeholder is derived from the folder."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference_number": {
                            "type": "string",
                            "description": "The matter's reference number (Aktenzeichen), "
                            "exactly as read from the document or path.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Short matter title in the document's language, "
                            "e.g. 'Projekt Falke — Unternehmenskauf'.",
                        },
                    },
                    "required": ["title"],
                },
                handler=run_create,
            )
        )
    return tools


def search_entities(session: Session, config: AppConfig, query: str, *, limit: int = 8) -> list[dict]:
    """Rank the firm's known parties/clients against a free-text name — the entity
    analogue of search_matters, and semantic-first for the same reason.

    Primary signal is SEMANTIC: embed the query, take the nearest already-indexed
    chunks, and surface the entities already resolved on those documents. A party is
    found by the meaning of where it appears, not by string overlap — so 'Nordwind
    Energie GmbH' and 'Nordwind Energie GmbH, Hamburg, HRB 45678' reach the same
    documents and therefore the same candidate, in any language, with no
    normalization rules. Lexical name and identifier matches are boosts, not the whole
    search. Skips the semantic leg cleanly on a cold index."""
    query = (query or "").strip()
    if not query:
        return []
    scored: dict[tuple[str, str], float] = {}  # (entity_type, id) -> score

    # Semantic: nearest already-indexed chunks -> their documents -> the entities
    # already resolved on those documents.
    try:
        from knowledge_index.search_backend import OpenSearchIndex

        vector = embed_text(query, config)
        for rank, hit in enumerate(OpenSearchIndex(config).matter_hits_by_vector(vector, size=40)):
            document_id = (hit.get("_source") or {}).get("document_id")
            document = session.get(Document, document_id) if document_id else None
            for mention in (document.parties or []) if document else []:
                entity_id = mention.get("party_id")
                entity_type = mention.get("entity_type")
                if entity_id and entity_type:
                    key = (entity_type, entity_id)
                    scored[key] = max(scored.get(key, 0.0), 1.0 / (10 + rank))
    except Exception:
        pass

    # Lexical: entity name contains the query (a boost).
    for party in session.scalars(select(Party).where(Party.name.ilike(f"%{query}%")).limit(30)):
        scored[("party", party.id)] = scored.get(("party", party.id), 0.0) + 0.5
    for client in session.scalars(select(Client).where(Client.name.ilike(f"%{query}%")).limit(30)):
        scored[("client", client.id)] = scored.get(("client", client.id), 0.0) + 0.5

    # Identifier: shared-identifier match (the entity analogue of the matter ref leg).
    for ident in session.scalars(
        select(EntityIdentifier).where(EntityIdentifier.value.ilike(f"%{query}%")).limit(30)
    ):
        key = (ident.entity_type, ident.entity_id)
        scored[key] = scored.get(key, 0.0) + 1.0

    ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)[:limit]
    results: list[dict] = []
    for (entity_type, entity_id), score in ranked:
        entity = session.get(Client if entity_type == "client" else Party, entity_id)
        if entity is None:
            continue
        results.append(
            {
                "id": entity.id,
                "entity_type": entity_type,
                "name": entity.name,
                "kind": entity.kind,
                "identifiers": entity.identifiers or {},
                "match_score": round(score, 4),
            }
        )
    return results


def party_resolution_tools(
    session: Session, config: AppConfig, seen_ids: set[str]
) -> list[AgentTool]:
    """The tool the extraction agent uses to resolve a party to a firm-wide entity:
    search the firm's known parties/clients (semantic-first, exactly like
    search_matters), then the agent links (reuses an id) or creates a new one.

    ``seen_ids`` accumulates every id the agent is shown, so the stage can reject an
    existing_id the agent never actually saw (the "name alone is not enough" guard).
    """

    def _search(args: dict) -> str:
        needle = str(args.get("query", "")).strip()
        results = search_entities(session, config, needle) if needle else []
        for row in results:
            seen_ids.add(row["id"])
        return json.dumps(results, ensure_ascii=False)

    return [
        AgentTool(
            name="search_entities",
            description=(
                "Search the firm's already-known parties and clients. Ranks by semantic "
                "similarity of where entities appear, plus name and identifier matches. "
                "Returns candidates with id, entity_type (party|client), name, kind and "
                "identifiers. Call it before deciding a party is new: reuse a candidate's "
                "id (as existing_id) ONLY when it is genuinely the same real-world entity "
                "— a matching name alone is not enough, because different companies share "
                "names."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Party name or identifier to search for.",
                    }
                },
                "required": ["query"],
            },
            handler=_search,
        )
    ]
