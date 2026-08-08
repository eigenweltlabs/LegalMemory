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
import re
from collections.abc import Callable

from sqlalchemy import func, select, text
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
from knowledge_index.retrieval_types import Page

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


def search_entities(
    session: Session, config: AppConfig, query: str, *, limit: int = 8, offset: int = 0
) -> list[dict]:
    """Rank the firm's known parties/clients against a free-text name — the entity
    analogue of search_matters, and semantic-first for the same reason.

    Primary signal is SEMANTIC: embed the query, take the nearest already-indexed
    chunks, and surface the entities already resolved on those documents. A party is
    found by the meaning of where it appears, not by string overlap — so 'Nordwind
    Energie GmbH' and 'Nordwind Energie GmbH, Hamburg, HRB 45678' reach the same
    documents and therefore the same candidate, in any language, with no
    normalization rules. Lexical name and identifier matches are boosts, not the whole
    search. Skips the semantic leg cleanly on a cold index."""
    return Page.slice(
        _ranked_entities(session, config, query), offset=max(0, offset), limit=limit
    ).items


def search_entities_page(
    session: Session, config: AppConfig, query: str, *, limit: int = 8, offset: int = 0
) -> Page:
    """``search_entities`` with an exact candidate total."""
    return Page.slice(
        _ranked_entities(session, config, query), offset=max(0, offset), limit=limit
    )


def _ranked_entities(session: Session, config: AppConfig, query: str) -> list[dict]:
    """Every party/client that matched, best first — what the pages are cut from."""
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
    for party in session.scalars(
        select(Party)
        .where(Party.name.ilike(f"%{query}%"))
        .order_by(Party.name, Party.id)
        .limit(30)
    ):
        scored[("party", party.id)] = scored.get(("party", party.id), 0.0) + 0.5
    for client in session.scalars(
        select(Client)
        .where(Client.name.ilike(f"%{query}%"))
        .order_by(Client.name, Client.id)
        .limit(30)
    ):
        scored[("client", client.id)] = scored.get(("client", client.id), 0.0) + 0.5

    # Identifier: shared-identifier match (the entity analogue of the matter ref leg).
    for ident in session.scalars(
        select(EntityIdentifier)
        .where(EntityIdentifier.value.ilike(f"%{query}%"))
        .order_by(EntityIdentifier.value, EntityIdentifier.id)
        .limit(30)
    ):
        key = (ident.entity_type, ident.entity_id)
        scored[key] = scored.get(key, 0.0) + 1.0

    # (type, id) breaks score ties, so pages cut in the same place every call.
    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
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


_ENTITY_NAME_SUFFIXES = frozenset(
    "ag gmbh kg se sa nv bv ab oy plc llp llc lp ltd inc corp co pc pllc "
    "gbr ohg ug mbh sarl sas spa srl".split()
)


def _normalize_entity_name(text: str) -> str:
    """Lowercased, punctuation-free, trailing-legal-suffix-free form of a name.

    Used ONLY to judge whether a search_entities query plausibly looked for a
    party — never to merge entities (merging stays the agent's call, and the
    deterministic net in ``_resolve_document_parties`` matches verbatim names)."""
    tokens = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    while tokens and tokens[-1] in _ENTITY_NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def entity_search_covered(name: str, searched_queries: set[str]) -> bool:
    """True when one of the agent's search_entities queries covered this party:
    after normalization, one string contains the other ("Vantage Prime Bank"
    covers "Vantage Prime Bank AG"). A name that normalizes to nothing needs no
    search."""
    normalized = _normalize_entity_name(name)
    if not normalized:
        return True
    for query in searched_queries:
        candidate = _normalize_entity_name(query)
        if candidate and (candidate in normalized or normalized in candidate):
            return True
    return False


def party_resolution_tools(
    session: Session,
    config: AppConfig,
    seen_ids: set[str],
    searched_queries: set[str] | None = None,
) -> list[AgentTool]:
    """The tool the extraction agent uses to resolve a party to a firm-wide entity:
    search the firm's known parties/clients (semantic-first, exactly like
    search_matters), then the agent links (reuses an id) or creates a new one.

    ``seen_ids`` accumulates every id the agent is shown, so the stage can reject an
    existing_id the agent never actually saw (the "name alone is not enough" guard).
    ``searched_queries`` accumulates every query the agent ran, so the stage can
    equally reject a CREATE for a party the agent never searched — without it,
    create is the frictionless default and the entity layer fills with twins
    (the 2026-08-01 audit: 63% duplicate party rows, 23.7% reuse)."""

    def _search(args: dict) -> str:
        needle = str(args.get("query", "")).strip()
        if searched_queries is not None and needle:
            searched_queries.add(needle)
        page = (
            search_entities_page(
                session,
                config,
                needle,
                limit=int(args.get("limit", 8) or 8),
                offset=int(args.get("offset", 0) or 0),
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
                "Search the firm's already-known parties and clients. Ranks by semantic "
                "similarity of where entities appear, plus name and identifier matches. "
                "Returns candidates with id, entity_type (party|client), name, kind and "
                "identifiers. Call it before deciding a party is new: reuse a candidate's "
                "id (as existing_id) ONLY when it is genuinely the same real-world entity "
                "— a matching name alone is not enough, because different companies share "
                "names."
                + _AGENT_PAGINATION
                + " Creating a duplicate of an entity that was simply on page 2 is the "
                "most expensive mistake here, so check has_more before deciding a party "
                "is new."
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
