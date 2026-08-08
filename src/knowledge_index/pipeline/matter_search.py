"""Ingestion-time search: the tools the classification and extraction agents use to
link a document to one of the firm's existing matters, and a named party to one of
its existing entities.

A firm has hundreds to thousands of matters, so the old "dump the first 80 matters
into the prompt" approach silently drops most of them. Instead the agent SEARCHES:
semantically over already-indexed chunks, lexically over matter titles, and by exact
reference number, then reads the folder neighbourhood the way a paralegal would. All
of this is unscoped by design — classification legitimately needs corpus-wide
visibility to find the right matter — and never runs on the user query path.

Entity resolution lives here too, and deliberately does NOT share the semantic leg:
see the block comment above ``EntityCandidate`` for what that cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
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
    MatterClient,
    MatterParty,
    Party,
    Project,
    SourceObject,
)
from knowledge_index.entity_names import (
    name_similarity,
    name_tokens,
    normalize_entity_name,
)
from knowledge_index.pipeline.folder_context import (
    _parent,
    folder_ls,
    list_one_folder,
    revisions_digest,
)
from knowledge_index.pipeline.providers import AgentTool, embed_text
from knowledge_index.retrieval_types import Page

log = logging.getLogger(__name__)

# The classification and relation agents see the same pagination contract as the
# external MCP surface, in fewer words: these agents run per document, thousands
# of times, and every token of tool description is paid on each one.
_AGENT_PAGINATION = (
    " Returns {results, page}; when page.has_more is true, more candidates exist "
    "— call again with offset=page.next_offset before concluding there are none."
)


def _agent_page(page: Page) -> str:
    """The JSON an agent tool hands back for a paginated result."""
    return json.dumps({"results": page.items, "page": page.as_dict()}, ensure_ascii=False)


def search_documents(
    session: Session, config: AppConfig, query: str, *, limit: int = 10, offset: int = 0
) -> list[dict]:
    """Find documents anywhere in the index by title or topic — used by the relation
    agent to locate a referenced master contract, judgment, or exhibit filed under a
    different folder or matter. Returns metadata only (no full text)."""
    return Page.slice(
        _ranked_documents(session, config, query), offset=max(0, offset), limit=limit
    ).items


def search_documents_page(
    session: Session, config: AppConfig, query: str, *, limit: int = 10, offset: int = 0
) -> Page:
    """``search_documents`` with an exact candidate total."""
    return Page.slice(
        _ranked_documents(session, config, query), offset=max(0, offset), limit=limit
    )


def _ranked_documents(session: Session, config: AppConfig, query: str) -> list[dict]:
    """Every document that matched, best first — the set the pages are cut from."""
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
        select(Document)
        .where(Document.title.ilike(f"%{query}%"))
        .order_by(Document.title, Document.id)
        .limit(20)
    ):
        scored[document.id] = scored.get(document.id, 0.0) + 0.4

    results: list[dict] = []
    # Document id breaks score ties, so the same query cuts pages in the same place.
    for document_id, _score in sorted(scored.items(), key=lambda item: (-item[1], item[0])):
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
    end = min(len(text), offset + max_chars)
    result = {
        "ref": source_object.id,
        "path": source_object.path,
        "filename": source_object.name,
        "offset": offset,
        "returned_chars": max(0, end - offset),
        "total_chars": len(text),
        # The agent had to derive continuation from offset + returned_chars <
        # total_chars, which is exactly the arithmetic a reader skips — so a long
        # file read once looked like a file read whole.
        "has_more": end < len(text),
        "next_offset": end if end < len(text) else None,
        "text": text[offset:end],
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
            return _agent_page(
                search_documents_page(
                    session,
                    config,
                    str(args.get("query", "")),
                    limit=int(args.get("limit", 10) or 10),
                    offset=int(args.get("offset", 0) or 0),
                )
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
                "or matter. Best match first."
                + _AGENT_PAGINATION
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Documents per page (default 10).",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Skip this many documents; use page.next_offset.",
                    },
                },
                "required": ["query"],
            },
            handler=find_documents,
        ),
        AgentTool(
            name="open_file",
            description=(
                "Open a file from the supplied directory listing by its exact path. Returns "
                "the stable source ref required for relationships plus converted text. The "
                "text is paginated by character: while has_more is true you are holding only "
                "the START of the file, so call again with offset=next_offset before "
                "concluding a reference or party is not in it."
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


def search_matters(
    session: Session,
    config: AppConfig,
    query: str,
    *,
    limit: int = 8,
    offset: int = 0,
    include_semantic: bool = True,
) -> list[dict]:
    """Rank existing matters against a free-text query (party, ref, title, topic).

    ``include_semantic=False`` skips the embedding leg for callers that must stay
    cheap — the create-time replay runs under the matter-create lock, and an
    embedding call per holder serialized the whole cold-start classify wave
    (measured 2026-08-01: 1,057 connections queued on the advisory lock).

    Every candidate is scored before the window is cut, so ``offset`` walks the
    same ranking rather than re-running a different one, and ``search_matters_page``
    can report how many candidates there were."""
    return Page.slice(
        _ranked_matters(session, config, query, include_semantic=include_semantic),
        offset=max(0, offset),
        limit=limit,
    ).items


def search_matters_page(
    session: Session,
    config: AppConfig,
    query: str,
    *,
    limit: int = 8,
    offset: int = 0,
    include_semantic: bool = True,
) -> Page:
    """``search_matters`` with an exact candidate total."""
    return Page.slice(
        _ranked_matters(session, config, query, include_semantic=include_semantic),
        offset=max(0, offset),
        limit=limit,
    )


def _ranked_matters(
    session: Session,
    config: AppConfig,
    query: str,
    *,
    include_semantic: bool = True,
) -> list[dict]:
    """Every matter that matched, best first — the set the pages are cut from."""
    query = (query or "").strip()
    if not query:
        return []
    scored: dict[str, float] = {}

    # Semantic: nearest already-indexed chunks -> their matters (skips cleanly when the
    # index is cold, i.e. nothing indexed yet on a fresh estate).
    if include_semantic:
        try:
            from knowledge_index.search_backend import OpenSearchIndex

            vector = embed_text(query, config)
            for rank, hit in enumerate(
                OpenSearchIndex(config).matter_hits_by_vector(vector, size=40)
            ):
                matter_id = (hit.get("_source") or {}).get("matter_id")
                if matter_id:
                    scored[matter_id] = max(scored.get(matter_id, 0.0), 1.0 / (10 + rank))
        except Exception:
            pass

    # Lexical: matter title contains the query.
    for matter in session.scalars(
        select(Matter)
        .where(Matter.title.ilike(f"%{query}%"))
        .order_by(Matter.title, Matter.id)
        .limit(30)
    ):
        scored[matter.id] = scored.get(matter.id, 0.0) + 0.5

    # Exact-ish: reference number substring.
    for matter_id in _matters_by_reference_substring(session, query):
        scored[matter_id] = scored.get(matter_id, 0.0) + 1.0

    # Matter id breaks score ties so the ranking — and therefore any page cut
    # from it — is the same on two calls with the same query.
    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    results: list[dict] = []
    for matter_id, score in ranked:
        matter = session.get(Matter, matter_id)
        if matter is None:
            continue
        summary = _matter_summary(session, matter)
        summary["match_score"] = round(score, 4)
        results.append(summary)
    return results


def _matters_by_reference_substring(session: Session, query: str) -> list[str]:
    """Ids of matters carrying a reference number containing ``query``.

    reference_numbers is a jsonb array, so on Postgres the substring test runs
    inside SQL over every matter. This used to be a Python scan over
    ``select(Matter).limit(2000)`` with no ORDER BY: past 2,000 matters, whether
    a given Aktenzeichen was findable at all depended on physical row order.
    SQLite has no jsonb functions, so tests keep the scan — ordered, and over the
    whole table, which is small in that setting.
    """
    needle = (query or "").strip().upper()
    if not needle:
        return []
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = text(
            "SELECT id FROM matters WHERE EXISTS ("
            "  SELECT 1 FROM jsonb_array_elements_text(matters.reference_numbers) AS ref"
            "  WHERE upper(ref) LIKE :needle"
            ") ORDER BY id"
        )
        return list(
            session.scalars(statement.bindparams(needle=f"%{needle}%")).all()
        )
    return [
        matter.id
        for matter in session.scalars(select(Matter).order_by(Matter.id))
        if any(needle in (ref or "").upper() for ref in (matter.reference_numbers or []))
    ]


def peek_matter(session: Session, matter_id: str, *, title_limit: int = 12) -> dict:
    matter = session.get(Matter, matter_id) if matter_id else None
    if matter is None:
        return {"error": "matter not found"}
    summary = _matter_summary(session, matter)
    titles = session.scalars(
        select(Document.title)
        .where(Document.matter_id == matter.id)
        .order_by(Document.title, Document.id)
        .limit(title_limit)
    ).all()
    summary["document_titles"] = [title for title in titles if title]
    # document_count is the matter's true document total; document_titles is at
    # most `title_limit` of them. Counted separately from member_documents, which
    # counts source-object assignments — a different number, and comparing the
    # two would mislabel a matter as sampled or as complete at random. Stated in
    # the payload so the agent does not read a 12-title list as the matter's
    # entire contents.
    summary["document_count"] = (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.matter_id == matter.id)
        )
        or 0
    )
    summary["document_titles_are_sample"] = summary["document_count"] > len(
        summary["document_titles"]
    )
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


def _matter_by_reference(
    session: Session, project_id: str | None, reference: str
) -> Matter | None:
    """Find the matter carrying this exact reference.

    reference_numbers is jsonb, so on Postgres this is a containment lookup the
    GIN index answers directly. This used to load every Matter row and scan it in
    Python while holding the matter-ref lock -- fine at 60 matters, quadratic
    across the ~1,300 this corpus creates, and the reason matter creation slowed
    to ~2 per five minutes. SQLite has no jsonb operator, so tests keep the scan.
    """
    reference = (reference or "").strip().upper()
    if not reference:
        return None
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = select(Matter).where(
            text("matters.reference_numbers @> cast(:ref as jsonb)")
        ).params(ref=json.dumps([reference]))
        if project_id:
            stmt = stmt.where(Matter.project_id == project_id)
        return session.scalars(stmt.limit(1)).first()
    return next(
        (
            item
            for item in session.scalars(select(Matter)).all()
            if reference in (item.reference_numbers or [])
            and (not project_id or item.project_id == project_id)
        ),
        None,
    )


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
        existing = _matter_by_reference(session, project_id, reference)
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
    # Enforced create protocol (2026-08-01 audit): the prompt's "create IMMEDIATELY
    # after your searches came up empty" was only a request, and a create acting on
    # a minutes-old empty search minted duplicate "splinter" matters when a sibling
    # created the real one in between. Track the call sequence so the tool can
    # reject stale creates, and remember the last queries so the create can replay
    # them at the true decision moment.
    last_call: list[str | None] = [None]
    last_queries: list[str] = []

    def run_search(args: dict) -> str:
        query = str(args.get("query", ""))
        page = search_matters_page(
            session,
            config,
            query,
            limit=int(args.get("limit", 8) or 8),
            offset=int(args.get("offset", 0) or 0),
        )
        seen.update(result["id"] for result in page.items)
        last_call[0] = "search_matters"
        if query.strip():
            last_queries.append(query.strip())
            del last_queries[:-3]  # keep the tail; older queries are stale anyway
        return _agent_page(page)

    def run_peek(args: dict) -> str:
        result = peek_matter(session, str(args.get("matter_id", "")))
        if "id" in result:
            seen.add(result["id"])
        last_call[0] = "peek_matter"
        return json.dumps(result, ensure_ascii=False)

    def run_list_folder(_args: dict) -> str:
        last_call[0] = "list_folder"
        return folder_ls(session, source_id, locus_path)

    def run_create(args: dict) -> str:
        assert session_factory is not None
        requested_reference = str(args.get("reference_number") or "").strip()
        title = str(args.get("title", "")).strip()
        if last_call[0] not in ("search_matters", "create_matter_refused"):
            last_call[0] = "create_matter"
            return json.dumps(
                {
                    "error": "stale_search",
                    "message": (
                        "create_matter must be your IMMEDIATELY NEXT action after "
                        "search_matters — your last search is stale and other documents "
                        "classify in parallel. Search again now, then create."
                    ),
                }
            )
        # Replay the agent's own recent queries (plus the exact reference and title
        # it is about to use) at the true decision moment, holding the create lock,
        # so two concurrent creators serialize and the later one is guaranteed to
        # see what the earlier one committed — regardless of which reference string
        # each model chose. The lock is sharded by the create key and the replay is
        # lexical-only: one global lock around an embedding call serialized the
        # whole cold-start classify wave (measured: 1,057 advisory-lock waiters).
        lock_key = (requested_reference or title).strip().upper()
        # Fast path, deliberately outside the lock: during the cold-start wave
        # most documents of a matter reach create_matter after some sibling has
        # already created it, and their only possible outcome is the refusal
        # below. Answering here keeps them off the lock queue entirely -- with
        # thousands of classify tasks live, that queue is what stalls the stage.
        # Correctness is unchanged: anything that passes this check still does
        # the authoritative replay under the lock.
        if requested_reference:
            prior = _matter_by_reference(session, project_id, requested_reference)
            if prior is not None and prior.id not in seen:
                summary = _matter_summary(session, prior)
                seen.add(prior.id)
                last_call[0] = "create_matter_refused"
                return json.dumps(
                    {
                        "error": "matter_list_changed",
                        "new_matters": [summary],
                        "message": (
                            "these matters were created since you searched — assign to "
                            "one if it is this document's matter, or call create_matter "
                            "again to confirm a genuinely new matter"
                        ),
                    },
                    ensure_ascii=False,
                )
        with session_factory() as replay_session:
            _advisory_xact_lock(
                replay_session, f"matter-create:{project_id or 'none'}:{lock_key}"
            )
            fresh: dict[str, dict] = {}
            for query in {*last_queries, requested_reference, title} - {""}:
                for row in search_matters(
                    replay_session, config, query, include_semantic=False
                ):
                    if row["id"] not in seen:
                        fresh[row["id"]] = row
            if fresh:
                seen.update(fresh)
                last_call[0] = "create_matter_refused"
                return json.dumps(
                    {
                        "error": "matter_list_changed",
                        "new_matters": list(fresh.values()),
                        "message": (
                            "these matters were created since you searched — assign to "
                            "one if it is this document's matter, or call create_matter "
                            "again to confirm a genuinely new matter"
                        ),
                    },
                    ensure_ascii=False,
                )
            # Create while the outer session still holds the create lock: a racing
            # creator blocks on the lock above and then replays against our commit.
            result = get_or_create_matter(
                session_factory,
                project_id=project_id,
                reference_number=requested_reference or (fallback_reference or ""),
                title=title,
                provenance=provenance,
                # a matter created under the folder-derived placeholder is a triage
                # pile, not a real case file — mark it so the UI can surface it
                status="unknown" if requested_reference else "unassigned",
            )
        if "id" in result:
            seen.add(result["id"])
        last_call[0] = "create_matter"
        return json.dumps(result, ensure_ascii=False)

    tools = [
        AgentTool(
            name="search_matters",
            description=(
                "Search the firm's existing matters by any text: a party name, a reference "
                "number (Aktenzeichen), a matter title, or a topic. Returns candidate matters "
                "with their reference numbers, titles, practice area and folders, best match "
                "first."
                + _AGENT_PAGINATION
                + " An empty results list means no matter matched — that is when to create "
                "one. A full page with has_more=true does NOT: page or re-query first, "
                "because creating a matter that already exists splits one file in two."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Party, reference number, title, or topic to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Candidates per page (default 8).",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Skip this many candidates; use page.next_offset.",
                    },
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
            handler=run_list_folder,
        ),
        AgentTool(
            name="peek_matter",
            description=(
                "Show one matter in detail by its id (from a search_matters result): title, "
                "reference numbers, practice area, folders and a sample of its document "
                "titles. document_count is the matter's true number of documents; "
                "document_titles is at most 12 of them, and "
                "document_titles_are_sample=true means you are seeing a sample."
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
                    "around, then create; a create that does not directly follow "
                    "search_matters is rejected as stale. Omit reference_number only when the "
                    "document and path truly show none; a stable placeholder is derived from "
                    "the folder."
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


# ---------------------------------------------------------------- entity resolution
#
# What the insertion agent used to be given, and why it created 1,212 clients for a
# corpus whose ground truth is 46. The lexical leg was `Party.name ILIKE '%query%'`,
# which cannot match "Nexford" against a stored "Nexford Industrial Holdings Inc." in
# either direction; the primary leg was semantic over already-INDEXED chunks, and
# `index` is the last pipeline stage, so during a bulk run it is empty exactly when
# extraction needs it (measured: extraction at 9,122 documents while index stood at
# 7,033, and near-empty for the first several thousand). The agent searched, was told
# nothing existed, and created — 973 times for a normalized name the estate already
# held, 293 of them within 60 seconds of the previous one.
#
# So: no leg here depends on the index stage. Identity comes from typed identifiers
# and from names, matched as token sets over a normalized column with a trigram index
# behind it. What the semantic leg used to buy — "Nordwind Energie GmbH, Hamburg, HRB
# 45678" reaching "Nordwind Energie GmbH" — the alias ledger buys instead, and keeps:
# every surface form that resolves to an entity is recorded on it and matches exactly
# from then on.

# Below this the match is not worth an agent's attention: a single shared generic
# token, or two names that merely rhyme. Candidates under it are dropped rather than
# ranked last, so `page.total` counts real candidates.
_ENTITY_SCORE_FLOOR = 0.35

# A containment match ("Verimark Group" inside "Verimark Hospitality Group Inc.")
# starts here. Strong enough to link with corroboration, never strong enough alone.
_ENTITY_LIKELY_SCORE = 0.80

# How many rows one leg may contribute before scoring. A generic token ("holdings")
# matches half the estate; the floor above sinks those, and this stops the leg from
# reading the whole table to find out.
_ENTITY_LEG_LIMIT = 60

# Matter references shown inline on a candidate. The true total rides along as
# matter_count and the sample says it is one, exactly like peek_matter's titles.
_ENTITY_MATTER_SAMPLE = 8

_ENTITY_VERDICTS = {
    "same_entity": (
        "the estate already holds this entity — reuse its id, do not create a second row"
    ),
    "likely": "same name family; link it when the evidence below agrees, else say so",
    "possible": "a weak name resemblance only",
    "distinct": (
        "shares the name but carries a CONFLICTING register identifier — a different "
        "company with the same name"
    ),
}


@dataclass(frozen=True)
class EntityCandidate:
    """One already-known entity that might be the one a document just named."""

    entity_type: str  # client | party
    entity_id: str
    name: str
    normalized_name: str
    kind: str
    score: float
    verdict: str
    components: dict[str, float]
    matched_identifiers: list[dict]
    conflicting_identifiers: list[dict]

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_type, self.entity_id)


def _entity_identifier_rows(session: Session, entity_type: str, entity_id: str) -> list[dict]:
    return [
        {"scheme": row.scheme, "value": row.value}
        for row in session.scalars(
            select(EntityIdentifier)
            .where(
                EntityIdentifier.entity_type == entity_type,
                EntityIdentifier.entity_id == entity_id,
            )
            .order_by(EntityIdentifier.scheme, EntityIdentifier.value)
        )
    ]


def _normalize_identifiers(identifiers: dict[str, str] | None) -> dict[str, str]:
    """The (scheme, value) pairs a mention brings, lowercased and trimmed for comparison."""
    return {
        str(scheme).strip().lower(): str(value).strip()
        for scheme, value in (identifiers or {}).items()
        if str(value or "").strip()
    }


def _entity_model(entity_type: str):
    return Client if entity_type == "client" else Party


def _candidate_pool(
    session: Session, query: str, identifiers: dict[str, str] | None
) -> dict[tuple[str, str], object]:
    """Every entity worth scoring for this query, from four independent legs.

    Generation is deliberately generous and scoring is strict: a leg that misses is
    a candidate that can never be found, while a leg that over-fetches only costs
    rows the score floor then drops.
    """
    normalized = normalize_entity_name(query)
    tokens = sorted(name_tokens(normalized), key=len, reverse=True)[:6]
    pool: dict[tuple[str, str], object] = {}
    postgres = session.bind is not None and session.bind.dialect.name == "postgresql"

    def collect(entity_type: str, rows) -> None:
        for row in rows:
            pool.setdefault((entity_type, row.id), row)

    # Leg 1 — typed identifiers. A shared register number is proof of identity, so
    # it must reach the pool whatever the names look like.
    wanted_values = {value.lower() for value in _normalize_identifiers(identifiers).values()}
    if query.strip():
        wanted_values.add(query.strip().lower())
    if wanted_values:
        for identifier in session.scalars(
            select(EntityIdentifier)
            .where(func.lower(EntityIdentifier.value).in_(sorted(wanted_values)))
            .order_by(EntityIdentifier.scheme, EntityIdentifier.value, EntityIdentifier.id)
            .limit(_ENTITY_LEG_LIMIT)
        ):
            entity = session.get(_entity_model(identifier.entity_type), identifier.entity_id)
            if entity is not None:
                pool.setdefault((identifier.entity_type, entity.id), entity)

    if not normalized:
        return pool

    for entity_type, model in (("client", Client), ("party", Party)):
        # Leg 2 — the identity key itself, and the alias ledger. Both are exact
        # lookups an index answers directly.
        collect(
            entity_type,
            session.scalars(
                select(model).where(model.normalized_name == normalized).limit(_ENTITY_LEG_LIMIT)
            ),
        )
        if postgres:
            collect(
                entity_type,
                session.scalars(
                    select(model)
                    .where(
                        text(f"{model.__tablename__}.normalized_aliases @> cast(:alias as jsonb)")
                    )
                    .params(alias=json.dumps([normalized]))
                    .limit(_ENTITY_LEG_LIMIT)
                ),
            )
            # Leg 3 — trigram similarity, for the variants nobody has recorded yet:
            # typos, dropped middle words, transliterations. `%` rides the GIN index
            # on normalized_name. Doubled because the driver's paramstyle is
            # `format`: a lone % in raw SQL reads as a placeholder.
            collect(
                entity_type,
                session.scalars(
                    select(model)
                    .where(text(f"{model.__tablename__}.normalized_name %% :needle"))
                    .params(needle=normalized)
                    .order_by(
                        func.similarity(model.normalized_name, normalized).desc(), model.id
                    )
                    .limit(_ENTITY_LEG_LIMIT)
                ),
            )
        # Leg 4 — one token at a time. This is the leg the old substring predicate
        # was missing: "Nexford" has to find "Nexford Industrial Holdings Inc." and
        # be found by it, and trigram similarity between a short name and a long one
        # is too low to clear any useful threshold. On Postgres the same trigram
        # index answers these LIKEs.
        for token in tokens:
            collect(
                entity_type,
                session.scalars(
                    select(model)
                    .where(model.normalized_name.like(f"%{token}%"))
                    .order_by(model.normalized_name, model.id)
                    .limit(_ENTITY_LEG_LIMIT)
                ),
            )
    return pool


def _score_candidate(
    session: Session,
    entity_type: str,
    entity,
    normalized_query: str,
    wanted_identifiers: dict[str, str],
) -> EntityCandidate | None:
    """Turn one pooled entity into a scored candidate, or drop it below the floor."""
    held = _entity_identifier_rows(session, entity_type, entity.id)
    held_by_scheme: dict[str, set[str]] = {}
    for row in held:
        held_by_scheme.setdefault(row["scheme"], set()).add(row["value"])
    matched = [
        {"scheme": scheme, "value": value}
        for scheme, value in sorted(wanted_identifiers.items())
        if value in held_by_scheme.get(scheme, set())
    ]
    conflicting = [
        {"scheme": scheme, "value": sorted(held_by_scheme[scheme])[0]}
        for scheme, value in sorted(wanted_identifiers.items())
        if scheme in held_by_scheme and value not in held_by_scheme[scheme]
    ]

    name_score, component = name_similarity(normalized_query, entity.normalized_name or "")
    alias_hit = normalized_query and normalized_query in (entity.normalized_aliases or [])
    if alias_hit and name_score < 1.0:
        name_score, component = 0.97, "alias"
    components: dict[str, float] = {}
    if component:
        components[component] = name_score
    if matched:
        components["identifier"] = 1.0

    if matched:
        score, verdict = 1.0, "same_entity"
    elif conflicting:
        # Same name, contradicting register number. Shown, never linked.
        score, verdict = name_score, "distinct"
    elif component == "exact" or alias_hit:
        score, verdict = max(name_score, 0.97), "same_entity"
    elif name_score >= _ENTITY_LIKELY_SCORE:
        score, verdict = name_score, "likely"
    elif name_score >= _ENTITY_SCORE_FLOOR:
        score, verdict = name_score, "possible"
    else:
        return None
    return EntityCandidate(
        entity_type=entity_type,
        entity_id=entity.id,
        name=entity.name,
        normalized_name=entity.normalized_name or "",
        kind=entity.kind,
        score=round(score, 4),
        verdict=verdict,
        components={key: round(value, 4) for key, value in components.items()},
        matched_identifiers=matched,
        conflicting_identifiers=conflicting,
    )


def entity_candidates(
    session: Session,
    query: str,
    *,
    identifiers: dict[str, str] | None = None,
) -> list[EntityCandidate]:
    """Every known client/party that could be ``query``, strongest first.

    The ranking is total and deterministic — score, then entity type, then id — so
    two calls with the same query cut a page in the same place.
    """
    normalized_query = normalize_entity_name(query)
    wanted = _normalize_identifiers(identifiers)
    if not normalized_query and not wanted and not (query or "").strip():
        return []
    scored: list[EntityCandidate] = []
    for (entity_type, _entity_id), entity in _candidate_pool(session, query, identifiers).items():
        candidate = _score_candidate(session, entity_type, entity, normalized_query, wanted)
        if candidate is not None:
            scored.append(candidate)
    scored.sort(key=lambda item: (-item.score, item.entity_type, item.entity_id))
    return scored


def _entity_matter_ids(session: Session, entity_type: str, entity_id: str) -> list[str]:
    """The matters an entity already appears on — the evidence that makes a link
    reviewable, and the reason cross-matter identity is the point: "show me
    everything for this client" is unanswerable while each matter mints its own copy."""
    if entity_type == "client":
        statement = select(MatterClient.matter_id).where(MatterClient.client_id == entity_id)
    else:
        statement = select(MatterParty.matter_id).where(MatterParty.party_id == entity_id)
    return sorted({matter_id for matter_id in session.scalars(statement) if matter_id})


def _candidate_row(
    session: Session, candidate: EntityCandidate, *, matter_id: str | None
) -> dict:
    """The tool payload for one candidate: what makes it the same entity, not just
    its name. Counts are exact; the matter list is a named sample of them."""
    entity = session.get(_entity_model(candidate.entity_type), candidate.entity_id)
    matter_ids = _entity_matter_ids(session, candidate.entity_type, candidate.entity_id)
    sample = matter_ids[:_ENTITY_MATTER_SAMPLE]
    references: list[str] = []
    for candidate_matter_id in sample:
        matter = session.get(Matter, candidate_matter_id)
        if matter is not None:
            references.append((matter.reference_numbers or [matter.title])[0])
    document_count = (
        session.scalar(
            select(func.count()).select_from(Document).where(Document.matter_id.in_(matter_ids))
        )
        or 0
        if matter_ids
        else 0
    )
    row = {
        "id": candidate.entity_id,
        "entity_type": candidate.entity_type,
        "name": candidate.name,
        "kind": candidate.kind,
        "aliases": list((entity.aliases or []) if entity is not None else []),
        "identifiers": _entity_identifier_rows(
            session, candidate.entity_type, candidate.entity_id
        ),
        "matched_identifiers": candidate.matched_identifiers,
        "conflicting_identifiers": candidate.conflicting_identifiers,
        "matter_count": len(matter_ids),
        "matter_refs": references,
        "matter_refs_are_sample": len(matter_ids) > len(sample),
        "document_count": document_count,
        "match": {
            "score": candidate.score,
            "verdict": candidate.verdict,
            "means": _ENTITY_VERDICTS[candidate.verdict],
            "components": candidate.components,
        },
    }
    if matter_id:
        row["appears_in_this_matter"] = matter_id in matter_ids
    return row


def search_entities_page(
    session: Session,
    query: str,
    *,
    limit: int = 8,
    offset: int = 0,
    identifiers: dict[str, str] | None = None,
    matter_id: str | None = None,
) -> Page:
    """One page of ranked candidates, each carrying its evidence.

    Scoring runs over the whole candidate set before the window is cut, so
    ``offset`` walks one ranking and ``page.total`` is the real number of
    candidates. The per-row evidence (identifiers, matters, counts) is gathered only
    for the rows actually returned."""
    page = Page.slice(
        entity_candidates(session, query, identifiers=identifiers),
        offset=max(0, offset),
        limit=limit,
    )
    page.items = [_candidate_row(session, item, matter_id=matter_id) for item in page.items]
    return page


def search_entities(
    session: Session,
    query: str,
    *,
    limit: int = 8,
    offset: int = 0,
    identifiers: dict[str, str] | None = None,
    matter_id: str | None = None,
) -> list[dict]:
    """``search_entities_page`` without the page block."""
    return search_entities_page(
        session,
        query,
        limit=limit,
        offset=offset,
        identifiers=identifiers,
        matter_id=matter_id,
    ).items


def entity_search_covered(name: str, searched_queries: set[str]) -> bool:
    """True when one of the agent's search_entities queries covered this party:
    after normalization, one name's tokens contain the other's ("Vantage Prime Bank"
    covers "Vantage Prime Bank AG"). A name that normalizes to nothing needs no
    search."""
    normalized = normalize_entity_name(name)
    if not normalized:
        return True
    wanted = name_tokens(normalized)
    for query in searched_queries:
        candidate = name_tokens(normalize_entity_name(query))
        if candidate and (candidate <= wanted or wanted <= candidate):
            return True
    return False


# ------------------------------------------------------------ creation without twins


def _entity_alias_ledger(entity, surface_name: str) -> None:
    """Record a surface form on the entity it resolved to.

    This is how the corpus teaches the resolver: the second document that writes
    "Nordwind Energie GmbH, Hamburg" resolves fuzzily, and every one after it
    resolves exactly."""
    normalized = normalize_entity_name(surface_name)
    if not normalized or normalized == (entity.normalized_name or ""):
        return
    aliases = list(entity.aliases or [])
    normalized_aliases = list(entity.normalized_aliases or [])
    if normalized in normalized_aliases:
        return
    normalized_aliases.append(normalized)
    if surface_name.strip() and surface_name.strip() not in aliases:
        aliases.append(surface_name.strip())
    # Reassigned rather than mutated: a JSON column tracks identity, not contents.
    entity.aliases = aliases
    entity.normalized_aliases = normalized_aliases


def _corroboration(
    session: Session,
    candidate: EntityCandidate,
    *,
    matter_id: str | None,
    sibling_entity_ids: set[str],
) -> list[str]:
    """Reasons beyond the name to believe a ``likely`` candidate is this entity.

    A shared identifier is proof and never reaches here. These are the signals that
    turn a strong name match into a link instead of a question: the entity is
    already on this matter, or it shares a matter with somebody else this very
    document names, or it acts for the same client this matter does."""
    reasons: list[str] = []
    matter_ids = set(_entity_matter_ids(session, candidate.entity_type, candidate.entity_id))
    if matter_id and matter_id in matter_ids:
        reasons.append("already_on_this_matter")
    if matter_ids and sibling_entity_ids:
        co_occurring = (
            session.scalar(
                select(func.count())
                .select_from(MatterParty)
                .where(
                    MatterParty.matter_id.in_(matter_ids),
                    MatterParty.party_id.in_(sibling_entity_ids),
                )
            )
            or 0
        ) + (
            session.scalar(
                select(func.count())
                .select_from(MatterClient)
                .where(
                    MatterClient.matter_id.in_(matter_ids),
                    MatterClient.client_id.in_(sibling_entity_ids),
                )
            )
            or 0
        )
        if co_occurring:
            reasons.append("shares_a_matter_with_another_party_on_this_document")
    if matter_id and matter_ids:
        this_matter_clients = set(
            session.scalars(
                select(MatterClient.client_id).where(MatterClient.matter_id == matter_id)
            )
        )
        if this_matter_clients:
            shared_client = session.scalar(
                select(func.count())
                .select_from(MatterClient)
                .where(
                    MatterClient.matter_id.in_(matter_ids - {matter_id}),
                    MatterClient.client_id.in_(this_matter_clients),
                )
            )
            if shared_client:
                reasons.append("acts_on_another_matter_for_this_matter_s_client")
    return reasons


def link_decision(
    session: Session,
    candidates: list[EntityCandidate],
    *,
    entity_type: str,
    matter_id: str | None,
    sibling_entity_ids: set[str],
) -> tuple[EntityCandidate | None, str, list[str]]:
    """THE RULE, in one place, so it can be read, argued with, and tested.

    A mention links to an entity the estate already holds when:

    1. they share a typed identifier — proof, and it outranks every name signal;
    2. the normalized names are identical, or the mention's name is already on the
       entity's alias ledger, and no register identifier contradicts it. This is the
       default path across matters, not a judgement the agent may decline: a firm
       client has many matters, and 1,076 of 1,212 clients touching exactly one
       matter is the inverse of what a firm looks like;
    3. one name contains the other token-for-token AND something beyond the name
       agrees (see ``_corroboration``).

    Everything else escalates: the candidates go back to the agent with their
    evidence and it decides. Creating alongside a candidate is allowed — different
    companies genuinely share names — but it is recorded on the new row's
    provenance, so "created a second entity for a name the estate already knows" is
    a countable event rather than a suspicion.

    Returns ``(entity, reason, corroboration)``; ``entity`` is None when nothing
    links automatically.
    """
    same_type = [item for item in candidates if item.entity_type == entity_type]
    ordered = same_type + [item for item in candidates if item.entity_type != entity_type]
    for candidate in ordered:
        if candidate.matched_identifiers:
            return candidate, "shared_identifier", []
    for candidate in ordered:
        if candidate.verdict == "same_entity":
            return candidate, "normalized_name", []
    for candidate in ordered:
        if candidate.verdict != "likely":
            continue
        reasons = _corroboration(
            session, candidate, matter_id=matter_id, sibling_entity_ids=sibling_entity_ids
        )
        if reasons:
            return candidate, "name_and_corroboration", reasons
    return None, "no_confident_candidate", []


def _identity_discriminator(
    candidates: list[EntityCandidate], identifiers: dict[str, str]
) -> str:
    """What makes a new row a legitimately DIFFERENT entity from a same-named one.

    Empty for almost everything. Non-empty only when the estate already holds this
    normalized name and this mention carries a register identifier that contradicts
    it — the one case where two rows with one name are the truth rather than the
    bug, and the only thing the unique constraint will let through."""
    conflicting = [item for item in candidates if item.conflicting_identifiers]
    if not conflicting or not identifiers:
        return ""
    scheme, value = sorted(identifiers.items())[0]
    return f"{scheme}:{value}"


def resolve_or_create_entity(
    session_factory: sessionmaker[Session],
    *,
    entity_type: str,
    name: str,
    kind: str,
    identifiers: dict[str, str],
    provenance: dict,
    matter_id: str | None = None,
    sibling_entity_ids: set[str] | None = None,
    preferred_entity_id: str | None = None,
) -> dict:
    """Resolve one named party to exactly one row, committed before this returns.

    Its own short session, for the same reason ``get_or_create_matter`` has one:
    thousands of documents extract in parallel and the entity has to become visible
    to the others the moment it exists, not when the calling stage's transaction
    commits minutes later. That alone removes the pure race — 293 of the 973
    duplicate creations happened within 60 seconds of the previous one.

    Two further guards, because "unlikely" is not "impossible":
    the advisory lock on the normalized name makes search-then-create atomic per
    name, and the unique constraint on (normalized_name, identity_discriminator)
    catches anything that reaches an insert anyway — a lock-key hash collision, a
    non-Postgres deployment, a future caller that forgets — by turning the losing
    insert into a re-select of the winner.
    """
    surface = (name or "").strip()
    normalized = normalize_entity_name(surface)
    identifiers = _normalize_identifiers(identifiers)
    with session_factory() as session:
        _advisory_xact_lock(session, f"entity:{entity_type}:{normalized}")
        candidates = entity_candidates(session, surface, identifiers=identifiers)
        entity, reason, corroboration = link_decision(
            session,
            candidates,
            entity_type=entity_type,
            matter_id=matter_id,
            sibling_entity_ids=sibling_entity_ids or set(),
        )
        resolved = None
        resolved_type = entity_type
        if entity is not None:
            resolved = session.get(_entity_model(entity.entity_type), entity.entity_id)
            resolved_type = entity.entity_type
        elif preferred_entity_id:
            # The agent named a candidate the deterministic rule did not reach. It
            # saw the evidence; honour it, and say which one said so. It may well have
            # named a row from the other table — the same company is the client here
            # and the counterparty there — so both are checked.
            other_type = "party" if entity_type == "client" else "client"
            for entity_type_candidate in (entity_type, other_type):
                resolved = session.get(_entity_model(entity_type_candidate), preferred_entity_id)
                if resolved is not None:
                    resolved_type = entity_type_candidate
                    reason = "agent_link"
                    break
        if resolved is not None:
            if resolved_type != entity_type:
                # The same real-world entity is the firm's client on one matter and a
                # counterparty on another; the two live in different tables. Carry the
                # identity across instead of minting an unrelated row for the twin.
                resolved = _mirror_entity(
                    session, resolved, entity_type=entity_type, provenance=provenance
                )
            _entity_alias_ledger(resolved, surface)
            for scheme, value in identifiers.items():
                _ensure_identifier(session, entity_type, resolved.id, scheme, value)
            session.commit()
            return {
                "id": resolved.id,
                "entity_type": entity_type,
                "created": False,
                "reason": reason,
                "corroboration": corroboration,
            }

        model = _entity_model(entity_type)
        discriminator = _identity_discriminator(candidates, identifiers)
        shadowed = [
            {"id": item.entity_id, "name": item.name, "score": item.score, "verdict": item.verdict}
            for item in candidates[:5]
        ]
        record = dict(provenance)
        record["resolution"] = {
            "decision": "created",
            "reason": reason,
            # Countable: `select count(*) from clients where
            # provenance #>> '{resolution,reason}' <> 'no_confident_candidate'`
            # is the number of times insertion made a second row for a name the
            # estate already knew.
            "shadowed_candidates": shadowed,
        }
        entity_row = model(
            name=surface,
            kind=kind,
            aliases=[],
            normalized_aliases=[],
            identity_discriminator=discriminator,
            identifiers=dict(identifiers),
            provenance=record,
        )
        session.add(entity_row)
        try:
            session.flush()
        except IntegrityError as conflict:
            session.rollback()
            return _reselect_after_conflict(
                session_factory,
                conflict,
                entity_type=entity_type,
                normalized=normalized,
                discriminator=discriminator,
                surface=surface,
                identifiers=identifiers,
            )
        for scheme, value in identifiers.items():
            _ensure_identifier(session, entity_type, entity_row.id, scheme, value)
        session.commit()
        if shadowed:
            log.info(
                "entity created alongside %d known candidate(s): %r (%s)",
                len(shadowed),
                surface,
                entity_type,
            )
        return {
            "id": entity_row.id,
            "entity_type": entity_type,
            "created": True,
            "reason": reason,
            "corroboration": [],
        }


def _reselect_after_conflict(
    session_factory: sessionmaker[Session],
    conflict: IntegrityError,
    *,
    entity_type: str,
    normalized: str,
    discriminator: str,
    surface: str,
    identifiers: dict[str, str],
) -> dict:
    """The loser of a race reads the winner's row instead of raising.

    Reached only when two creators got past the advisory lock — a non-Postgres
    deployment, or a key collision. The constraint decided; this reports what it
    decided."""
    model = _entity_model(entity_type)
    with session_factory() as session:
        winner = session.scalar(
            select(model).where(
                model.normalized_name == normalized,
                model.identity_discriminator == discriminator,
            )
        )
        if winner is None:  # pragma: no cover - the constraint fired for another reason
            raise conflict
        _entity_alias_ledger(winner, surface)
        for scheme, value in identifiers.items():
            _ensure_identifier(session, entity_type, winner.id, scheme, value)
        session.commit()
        return {
            "id": winner.id,
            "entity_type": entity_type,
            "created": False,
            "reason": "lost_creation_race",
            "corroboration": [],
        }


def _mirror_entity(session: Session, source, *, entity_type: str, provenance: dict):
    """The counterpart row for an entity that is a client here and a party there.

    Clients and parties are separate tables by design (docs/concepts/data-model.md),
    so one company can need a row in each. It gets the same name, aliases and
    identifiers, so both rows resolve from either spelling and the pair is
    recognizable as one entity rather than as two unrelated ones."""
    model = _entity_model(entity_type)
    existing = session.scalar(
        select(model).where(
            model.normalized_name == source.normalized_name,
            model.identity_discriminator == source.identity_discriminator,
        )
    )
    if existing is not None:
        return existing
    record = dict(provenance)
    record["resolution"] = {"decision": "mirrored", "from": source.id}
    mirror = model(
        name=source.name,
        kind=source.kind,
        aliases=list(source.aliases or []),
        normalized_aliases=list(source.normalized_aliases or []),
        identity_discriminator=source.identity_discriminator,
        identifiers=dict(source.identifiers or {}),
        provenance=record,
    )
    session.add(mirror)
    session.flush()
    for row in _entity_identifier_rows(session, "client" if entity_type == "party" else "party", source.id):
        _ensure_identifier(session, entity_type, mirror.id, row["scheme"], row["value"])
    return mirror


def _ensure_identifier(
    session: Session, entity_type: str, entity_id: str, scheme: str, value: str
) -> None:
    """Idempotent promotion of one typed identifier onto an entity."""
    scheme = str(scheme).strip().lower()
    value = str(value).strip()
    if not scheme or not value:
        return
    exists = session.scalar(
        select(EntityIdentifier).where(
            EntityIdentifier.entity_type == entity_type,
            EntityIdentifier.entity_id == entity_id,
            EntityIdentifier.scheme == scheme,
            EntityIdentifier.value == value,
        )
    )
    if exists is None:
        session.add(
            EntityIdentifier(
                entity_type=entity_type, entity_id=entity_id, scheme=scheme, value=value
            )
        )


def party_resolution_tools(
    session: Session,
    seen_ids: set[str],
    searched_queries: set[str] | None = None,
    *,
    matter_id: str | None = None,
) -> list[AgentTool]:
    """The tool the extraction agent uses to resolve a party to a firm-wide entity.

    ``seen_ids`` accumulates every id the agent is shown, so the stage can reject an
    existing_id the agent never actually saw. ``searched_queries`` accumulates every
    query it ran, so the stage can equally reject a CREATE for a party it never
    searched — without that, create is the frictionless default.

    Neither guard was ever the weak link, though. The tool told the truth about a
    search that could not find what it was looking for, and the agent believed it.
    """

    def _search(args: dict) -> str:
        needle = str(args.get("query", "")).strip()
        if searched_queries is not None and needle:
            searched_queries.add(needle)
        page = (
            search_entities_page(
                session,
                needle,
                limit=int(args.get("limit", 8) or 8),
                offset=int(args.get("offset", 0) or 0),
                matter_id=matter_id,
            )
            if needle
            else Page()
        )
        for row in page.items:
            seen_ids.add(row["id"])
        return _agent_page(page)

    return [
        AgentTool(
            name="search_entities",
            description=(
                "Search the firm's already-known parties and clients by name or register "
                "identifier. Matching is on the normalized name, so a shorter or longer "
                "form of the same name still finds it ('Nexford' finds 'Nexford Industrial "
                "Holdings Inc.'), and every name form already seen for an entity is "
                "searchable too. Each candidate arrives with the evidence: matching and "
                "conflicting identifiers, how many matters it appears on and which, "
                "whether it is already on THIS document's matter, and match.verdict — "
                "same_entity means the estate already holds it, so reuse its id; likely "
                "means judge it against the evidence; distinct means it shares the name "
                "but carries a conflicting register number and is a different company."
                + _AGENT_PAGINATION
                + " A firm client appears across many matters, so finding a candidate on "
                "another matter is normal and is a reason to link, not a reason to create."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Party name or identifier to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Candidates per page (default 8).",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Skip this many candidates; use page.next_offset.",
                    },
                },
                "required": ["query"],
            },
            handler=_search,
        )
    ]
