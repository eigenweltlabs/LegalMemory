"""Metadata-first retrieval with source ACL enforcement inside every query path."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session, attributes

from knowledge_index.config import AppConfig
from knowledge_index.entity_names import (
    name_similarity,
    normalize_entity_name,
    normalize_group,
)
from knowledge_index.db.models import (
    Artifact,
    Blob,
    DecisionRecord,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    EvalRecord,
    FirmPerson,
    FirmPracticeGroup,
    Matter,
    MatterClient,
    MatterParty,
    MatterTeam,
    Party,
    Project,
    Relation,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.permissions import AccessService, CompiledAccessScope
from knowledge_index.pipeline.providers import chat_json, embed_text, usage_stage
from knowledge_index.retrieval_types import Page, SearchFilters

# Smallest slice of ranked documents authorized per bulk round-trip. Each batch
# costs a fixed handful of set-based statements no matter how many rows it covers,
# so batches are sized generously: index rows that turn out stale or unauthorized
# must not force a second round for a page that one round could have filled.
_VERIFY_BATCH_MIN = 24

# How many leading hits the LLM rerank is allowed to reorder. Everything behind
# this keeps its fused order (and is still returned — see _rerank).
_RERANK_WINDOW = 20

# Deepest window (offset + limit) the ranked search path will serve. Ranked
# pagination is re-ranking, not a cursor: every page re-runs fusion over a
# candidate pool sized for the whole window, so cost grows with depth. Past this
# the honest answer is "narrow the filters", not a slower page — and saying so
# beats clamping, which is the silent truncation this whole change removes.
_MAX_RANKED_WINDOW = 500


@dataclass
class SearchHit:
    project_id: str | None
    document_id: str
    version_id: str
    matter_id: str | None
    title: str | None
    doc_type: str | None  # ontology node id
    version_status: str
    score: float
    excerpt: str
    doc_type_label: str | None = None  # human label resolved from the ontology
    doc_date: str | None = None  # extracted document date, ISO, drives recency choices
    language: str | None = None
    matter_ref: str | None = None  # human matter reference, e.g. 1038-00001
    parties: list[dict] = field(default_factory=list)  # [{name, role}]
    identifiers: list[str] = field(default_factory=list)  # the document's own legal ids
    # Version position within the document. A row is one VERSION of a document, and
    # without these three a caller cannot tell "the draft" from "the signed one":
    # it sees two independent-looking rows that differ only in filename, and cites
    # whichever it read first. Measured on graded runs, that is a recurring wrong
    # answer — a near-final draft cited while the final sat one ordinal later on
    # the same document.
    version_ordinal: int | None = None  # 1 = earliest known version
    is_latest_final: bool = False  # this version IS the document's authoritative one
    latest_final_version_id: str | None = None  # which version is, when this is not
    source_paths: list[str] = field(default_factory=list)
    matched_identifiers: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)

    def as_dict(self, *, include_match: bool = True) -> dict:
        """The row a list-shaped tool returns.

        ``include_match`` is False for the metadata filter, which has no query:
        there ``score`` is always 0.0, ``matched_identifiers`` always empty, and
        ``excerpt`` is the first 320 characters of whichever chunk happened to
        come back — a spreadsheet's column headers as often as anything
        responsive. Shipping those three invites the caller to read meaning into
        noise, and charges it tokens per row for the privilege.
        """
        row = {
            "document_id": self.document_id,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "matter_id": self.matter_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "doc_type_label": self.doc_type_label,
            "doc_date": self.doc_date,
            "language": self.language,
            "matter_ref": self.matter_ref,
            "parties": self.parties,
            "identifiers": self.identifiers,
            "version_status": self.version_status,
            "version_ordinal": self.version_ordinal,
            "is_latest_final": self.is_latest_final,
            "latest_final_version_id": self.latest_final_version_id,
            "source_paths": self.source_paths,
            # No embedded citation record: the row already carries the hit's
            # full identity (document_id, version_id, matter_id, source_paths)
            # — which is what a caller needs to open or cite it. The citation
            # record itself comes from get_document.
        }
        if include_match:
            row["score"] = round(self.score, 6)
            row["excerpt"] = self.excerpt
            row["matched_identifiers"] = self.matched_identifiers
        return row


@dataclass(frozen=True)
class DownloadableDocument:
    """One authorized original blob plus the provenance needed to export it safely."""

    document_id: str
    version_id: str
    source_object_id: str
    content_hash: str
    filename: str
    mime_type: str
    size_bytes: int
    cached_path: Path
    citation: dict

    def metadata(self) -> dict:
        return {
            "document_id": self.document_id,
            "version_id": self.version_id,
            "source_object_id": self.source_object_id,
            "content_hash": self.content_hash,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "citations": [self.citation],
        }


class RerankScore(BaseModel):
    id: str
    score: float = PydanticField(ge=0, le=10)


class RerankResult(BaseModel):
    scores: list[RerankScore]


#: Authority ordering used when collapse picks WHICH version of a document to
#: surface. Higher wins. This is the right home for supersession — inside one
#: document's chain — as opposed to scaling scores across unrelated documents.
_VERSION_STATUS_ORDER: dict[str, int] = {"executed": 3, "final": 2, "unknown": 1, "draft": 0}

@dataclass
class _Candidate:
    """One fused chunk-level candidate before SQL re-verify and collapse."""

    chunk_id: str
    source: dict
    fused_score: float = 0.0
    matched_identifiers: list[str] = field(default_factory=list)


class RetrievalService:
    def __init__(self, session: Session, config: AppConfig) -> None:
        self.session = session
        self.config = config
        # Filled by _warm_identity_map per materialized page; held on self so
        # the bulk-loaded rows stay strongly referenced while hits are built.
        self._warm_matters: dict[str, Matter] = {}
        self._warm_parties: dict[str, Party] = {}

    def search_filter(
        self,
        *,
        principals: set[str],
        filters: SearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        return self._search(
            query=None,
            principals=principals,
            filters=filters or SearchFilters(),
            limit=limit,
            offset=offset,
            scope=scope,
        )

    def search_filter_page(
        self,
        *,
        principals: set[str],
        filters: SearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        scope: CompiledAccessScope | None = None,
    ) -> Page:
        """``search_filter`` plus an exact ``has_more``, via one extra hit.

        Now that the index collapses per version, running out of rows means the
        result set ended rather than that the chunk window did — so a page that
        is not full also fixes ``total`` exactly, which is what a caller asking
        "what is in this matter" actually wants to know.
        """
        page = Page.probe(
            self.search_filter(
                principals=principals,
                filters=filters,
                limit=limit + 1,
                offset=offset,
                scope=scope,
            ),
            offset=offset,
            limit=limit,
        )
        if not page.has_more:
            page.total = offset + len(page.items)
        return page

    def suggest_for_empty(
        self,
        *,
        principals: set[str],
        filters: SearchFilters | None = None,
        scope: CompiledAccessScope | None = None,
    ) -> dict[str, list[str]]:
        """What the caller could have filtered on, when its filters matched nothing.

        An empty result is the least useful thing a search can say: it does not
        distinguish "wrong spelling" from "not in this matter" from "you cannot see
        it". Returns near-miss values for each filter that was set, inside the
        caller's own access scope, so the next call can be corrected rather than
        guessed.
        """
        from knowledge_index.search_backend import OpenSearchIndex

        filters = self._resolve_practice_area(
            self._resolve_matter_filters(filters or SearchFilters())
        )
        if scope is None:
            scope = AccessService(self.session).compile_scope(
                principals,
                project_ids=[filters.project_id] if filters.project_id else [],
            )
        return OpenSearchIndex(self.config).suggest_filter_values(scope=scope, filters=filters)

    def search_semantic(
        self,
        query: str,
        *,
        principals: set[str],
        filters: SearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("semantic query must not be empty")
        return self._search(
            query=query,
            principals=principals,
            filters=filters or SearchFilters(),
            limit=limit,
            offset=offset,
            scope=scope,
        )

    def search_semantic_page(
        self,
        query: str,
        *,
        principals: set[str],
        filters: SearchFilters | None = None,
        limit: int = 20,
        offset: int = 0,
        scope: CompiledAccessScope | None = None,
    ) -> Page:
        """``search_semantic`` plus an exact ``has_more``, via one extra hit."""
        return Page.probe(
            self.search_semantic(
                query,
                principals=principals,
                filters=filters,
                limit=limit + 1,
                offset=offset,
                scope=scope,
            ),
            offset=offset,
            limit=limit,
        )

    def get_document(
        self,
        document_id: str,
        *,
        principals: set[str],
        version_id: str | None = None,
    ) -> dict | None:
        selected = self._select_document_version(document_id, version_id)
        if selected is None:
            return None
        document, version = selected
        sources = self._authorized_sources(version.id, principals)
        if not sources:
            return None
        citation = self._citation(document, version, sources)
        artifact = self.session.scalar(
            select(Artifact)
            .where(
                Artifact.content_hash == version.content_hash,
                Artifact.kind == "structured_json",
            )
            .order_by(Artifact.created_at.desc())
        )
        return {
            "document": {
                "id": document.id,
                "project_id": document.project_id,
                "matter_id": document.matter_id,
                "title": document.title,
                "doc_type": document.doc_type,
                "language": document.language,
                "doc_date": document.doc_date.isoformat() if document.doc_date else None,
            },
            "version": {
                "id": version.id,
                "ordinal": version.ordinal,
                "status": version.status,
                "content_hash": version.content_hash,
            },
            "content": artifact.payload if artifact else None,
            "project": citation["project"],
            "sources": citation["source_objects"],
            "citations": [citation],
        }

    def get_downloadable_document(
        self,
        document_id: str,
        *,
        principals: set[str],
        version_id: str | None = None,
        source_object_id: str | None = None,
    ) -> DownloadableDocument | None:
        """Resolve an authorized document version to its exact cached original bytes.

        Authorization is checked against the source object, not merely the document.
        The returned cache path is internal-only and must never be exposed as a client
        filesystem path.
        """

        selected = self._select_document_version(document_id, version_id)
        if selected is None:
            return None
        document, version = selected
        sources = self._authorized_sources(version.id, principals)
        if source_object_id is not None:
            sources = [source for source in sources if source.id == source_object_id]
        if not sources:
            return None
        source = sorted(sources, key=lambda item: (item.path, item.id))[0]
        blob = self.session.get(Blob, version.content_hash)
        if blob is None or not blob.cached_path:
            return None
        cached_path = Path(blob.cached_path).expanduser().resolve()
        artifact_root = self.config.artifact_dir.expanduser().resolve()
        if not cached_path.is_relative_to(artifact_root) or not cached_path.is_file():
            return None
        filename = Path(source.name or source.path).name.strip() or f"{document.id}.bin"
        declared_mime = source.mime_type or blob.mime_sniffed
        guessed_mime = _guess_mime_type(filename)
        mime_type = (
            guessed_mime
            if not declared_mime or declared_mime == "application/octet-stream"
            else declared_mime
        ) or "application/octet-stream"
        citation = self._citation(document, version, [source])
        return DownloadableDocument(
            document_id=document.id,
            version_id=version.id,
            source_object_id=source.id,
            content_hash=version.content_hash,
            filename=filename,
            mime_type=mime_type,
            size_bytes=blob.size_bytes,
            cached_path=cached_path,
            citation=citation,
        )

    def find_related_documents(
        self,
        document_id: str,
        *,
        principals: set[str],
        include_same_matter: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict | None:
        """Return graph-ready document context with explicit relation provenance.

        Stored graph edges remain distinguishable from deterministic context edges
        derived from a shared matter or thread.  Every returned document is independently
        authorization-checked and carries an exact citation.

        The whole related set is resolved and authorized before it is paged, so
        ``page.total`` is the exact number of visible related documents and the
        edge lists are consistent with the documents on the returned page.
        """

        root = self.session.get(Document, document_id)
        root_summary = self._document_summary(root, principals) if root else None
        if root is None or root_summary is None:
            return None

        reasons: dict[str, list[dict]] = {}
        explicit_edges: list[dict] = []

        def add_reason(related_id: str, reason: dict) -> None:
            if related_id != root.id:
                reasons.setdefault(related_id, []).append(reason)

        direct = self.session.scalars(
            select(Relation).where(
                (
                    (Relation.from_type == "document")
                    & (Relation.from_id == root.id)
                )
                | ((Relation.to_type == "document") & (Relation.to_id == root.id))
            )
        ).all()
        thread_ids: set[str] = set()
        for relation in direct:
            if relation.from_type == "document" and relation.from_id == root.id:
                other_type, other_id, direction = relation.to_type, relation.to_id, "outgoing"
            else:
                other_type, other_id, direction = (
                    relation.from_type,
                    relation.from_id,
                    "incoming",
                )
            if other_type == "document":
                add_reason(
                    other_id,
                    {
                        "basis": "stored_relation",
                        "kind": relation.kind,
                        "direction": direction,
                        "provenance": relation.provenance,
                    },
                )
            elif other_type == "document_version":
                other_version = self.session.get(DocumentVersion, other_id)
                if other_version:
                    add_reason(
                        other_version.document_id,
                        {
                            "basis": "stored_relation",
                            "kind": relation.kind,
                            "direction": direction,
                            "provenance": relation.provenance,
                        },
                    )
            elif other_type == "thread":
                thread_ids.add(other_id)
            explicit_edges.append(
                {
                    "kind": relation.kind,
                    "from": {"type": relation.from_type, "id": relation.from_id},
                    "to": {"type": relation.to_type, "id": relation.to_id},
                    "provenance": relation.provenance,
                }
            )

        if thread_ids:
            thread_relations = self.session.scalars(
                select(Relation).where(
                    Relation.kind == "belongs_to_thread",
                    Relation.from_type == "document",
                    Relation.to_type == "thread",
                    Relation.to_id.in_(thread_ids),
                )
            ).all()
            for relation in thread_relations:
                add_reason(
                    relation.from_id,
                    {
                        "basis": "shared_thread",
                        "kind": "belongs_to_thread",
                        "thread_id": relation.to_id,
                        "provenance": relation.provenance,
                    },
                )

        if include_same_matter and root.matter_id:
            matter_document_ids = self.session.scalars(
                select(Document.id).where(Document.matter_id == root.matter_id)
            ).all()
            for related_id in matter_document_ids:
                add_reason(
                    related_id,
                    {
                        "basis": "shared_matter",
                        "kind": "same_matter",
                        "matter_id": root.matter_id,
                    },
                )

        related: list[dict] = []
        for related_id, document_reasons in reasons.items():
            document = self.session.get(Document, related_id)
            summary = self._document_summary(document, principals) if document else None
            if summary is None:
                continue
            related.append({**summary, "relationships": document_reasons})
        related.sort(
            key=lambda item: (
                not any(
                    reason["basis"] == "stored_relation"
                    for reason in item["relationships"]
                ),
                item.get("title") or "",
                item["document_id"],
            )
        )
        # Page after sorting: `related` holds every visible related document, so
        # the count below is exact rather than "as many as the limit allowed".
        page = Page.slice(related, offset=max(0, offset), limit=max(0, limit))
        related = page.items
        visible_ids = {item["document_id"] for item in related} | {root.id}
        # Edges carry the relation and its provenance (the evidence that
        # established it) — not the endpoints' citation records. Both endpoints
        # are visible by construction; a caller wanting a citation opens the
        # endpoint with get_document.
        visible_explicit_edges: list[dict] = []
        for edge in explicit_edges:
            if (
                edge["from"]["type"] == "document"
                and edge["from"]["id"] not in visible_ids
            ) or (
                edge["to"]["type"] == "document"
                and edge["to"]["id"] not in visible_ids
            ):
                continue
            visible_explicit_edges.append({**edge, "basis": "stored_relation"})
        context_edges = [
            {
                "kind": reason["kind"],
                "basis": reason["basis"],
                "from": {"type": "document", "id": root.id},
                "to": {"type": "document", "id": item["document_id"]},
                "provenance": {
                    key: value
                    for key, value in reason.items()
                    if key not in {"kind", "basis"}
                },
            }
            for item in related
            for reason in item["relationships"]
            if reason["basis"] != "stored_relation"
        ]
        return {
            "root_document": root_summary,
            "related_documents": related,
            "edges": [*visible_explicit_edges, *context_edges],
            "explicit_edges": visible_explicit_edges,
            "include_same_matter": include_same_matter,
            # Rows on THIS page. page.total is how many exist in total — the two
            # differ exactly when has_more is true.
            "result_count": len(related),
            "page": page.as_dict(),
        }

    def traverse(
        self,
        entity_type: str,
        entity_id: str,
        *,
        principals: set[str],
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Stored relation edges whose BOTH endpoints the caller may see.

        The limit is applied to visible edges, not to the rows read from SQL.
        It used to be a SQL ``LIMIT`` ahead of the visibility filter, so a caller
        asking for 100 edges got however many of the first 100 rows happened to
        be visible — often far fewer, with no way to reach the rest. Rows are now
        read in ordered batches and filtered as they come, until the requested
        page is full or the edges run out.
        """
        if not self._entity_visible(entity_type, entity_id, principals):
            return []
        relations = self._visible_relations(
            entity_type, entity_id, principals=principals, limit=limit, offset=offset
        )
        visible: list[dict] = []
        for relation in relations:
            # Edge rows are collection rows: relation, endpoints, provenance.
            # Both endpoints passed the visibility filter above; their citation
            # records belong to the item-level tools, not to every edge that
            # mentions them.
            visible.append(
                {
                    "kind": relation.kind,
                    "from": {"type": relation.from_type, "id": relation.from_id},
                    "to": {"type": relation.to_type, "id": relation.to_id},
                    "provenance": relation.provenance,
                }
            )
        return visible

    def traverse_page(
        self,
        entity_type: str,
        entity_id: str,
        *,
        principals: set[str],
        limit: int = 100,
        offset: int = 0,
    ) -> Page:
        """``traverse`` plus an exact ``has_more``, via one extra visible edge."""
        return Page.probe(
            self.traverse(
                entity_type,
                entity_id,
                principals=principals,
                limit=limit + 1,
                offset=offset,
            ),
            offset=offset,
            limit=limit,
        )

    def _visible_relations(
        self,
        entity_type: str,
        entity_id: str,
        *,
        principals: set[str],
        limit: int,
        offset: int,
    ) -> list[Relation]:
        """Relation rows with both endpoints visible, filtered before the limit.

        Ordered by id so a page is reproducible; the underlying query has no
        natural order and an unordered offset can repeat and skip rows.
        """
        if limit <= 0:
            return []
        statement = (
            select(Relation)
            .where(
                ((Relation.from_type == entity_type) & (Relation.from_id == entity_id))
                | ((Relation.to_type == entity_type) & (Relation.to_id == entity_id))
            )
            .order_by(Relation.id)
        )
        wanted = offset + limit
        kept: list[Relation] = []
        scanned = 0
        batch_size = max(wanted, _VERIFY_BATCH_MIN)
        while len(kept) < wanted:
            batch = self.session.scalars(
                statement.offset(scanned).limit(batch_size)
            ).all()
            if not batch:
                break
            scanned += len(batch)
            for relation in batch:
                if not self._entity_visible(
                    relation.from_type, relation.from_id, principals
                ):
                    continue
                if not self._entity_visible(
                    relation.to_type, relation.to_id, principals
                ):
                    continue
                kept.append(relation)
                if len(kept) >= wanted:
                    break
            if len(batch) < batch_size:
                break
        return kept[offset:]

    def matter_visible(self, matter_id: str, principals: set[str]) -> bool:
        """Whether the caller can see any document version filed under a matter.

        The billing tools used to answer this by listing the first 1,000 matters
        by title and testing membership, so a matter sorting after that prefix
        was reported as unauthorized to a caller who could in fact read it.
        """
        return bool(matter_id) and bool(self.citations_for_matter(matter_id, principals))

    def list_matters(
        self,
        *,
        principals: set[str],
        limit: int = 100,
        offset: int = 0,
        practice_area: str | None = None,
        matter_kind: str | None = None,
        lifecycle: str | None = None,
        practice_group: str | None = None,
        firm_person: str | None = None,
        include_documents: bool = False,
    ) -> list[dict]:
        """Matters visible to the caller; ``practice_area`` filters by ontology
        node with SUBTREE semantics (a parent area matches its children), and
        ``lifecycle`` restricts to matters in a given state (executed, closed,
        terminated, dormant, in_progress).

        Ordered by title, and the limit counts matters the caller can actually
        see. The limit used to be a SQL ``LIMIT`` on all matters, applied before
        both the practice-area filter and the per-matter visibility check, so
        ``limit=100`` on a corpus of 1,300 matters examined the first 100 titles
        and returned whichever subset survived — everything alphabetically later
        was unreachable at any limit. Rows are now scanned in ordered batches and
        filtered as they come, until the page is full or the matters run out.
        """
        if limit <= 0:
            return []
        try:
            area_scope = self.config.ontology_facet("area_of_law")
            service_scope = self.config.ontology_facet("service")
        except ValueError:
            area_scope = None
            service_scope = None

        statement = select(Matter).order_by(Matter.title, Matter.id)
        if lifecycle is not None:
            statement = statement.where(Matter.lifecycle == lifecycle)
        if practice_group is not None:
            # Matches ANY group working the matter, not only the owning partner's.
            # A financing staffed by Banking & Finance with Tax and Real Estate
            # alongside is a Tax matter to the tax partner asking what their group
            # has touched, and answering "no" because the responsible partner sits
            # elsewhere is how a practice loses sight of its own work. The matter's
            # own group still counts, so the owner is never missed when the team is
            # unrecorded.
            #
            # Case- and ampersand-insensitive: the firm writes "Banking & Finance",
            # a caller may type "Banking and Finance", and neither should miss.
            names = self._group_spellings(practice_group)
            if not names:
                return []
            statement = statement.where(
                or_(
                    Matter.practice_group.in_(names),
                    Matter.id.in_(
                        select(MatterTeam.matter_id)
                        .join(FirmPerson, FirmPerson.id == MatterTeam.person_id)
                        .where(FirmPerson.practice_group.in_(names))
                    ),
                )
            )
        if firm_person is not None:
            # "Which matters does Merritt run" — matched on the resolver's own
            # normalisation so a surname, a full name and a differently-punctuated
            # spelling all land on the same lawyer.
            statement = statement.where(
                Matter.id.in_(
                    select(MatterTeam.matter_id)
                    .join(FirmPerson, FirmPerson.id == MatterTeam.person_id)
                    .where(self._person_name_matches(firm_person))
                )
            )
        if practice_area is not None:
            # SUBTREE semantics pushed into SQL: the node's descendants are a set
            # the ontology can enumerate once, instead of an ancestors() call per
            # scanned row. An unresolvable facet or an empty subtree matches
            # nothing, exactly as the per-row check did.
            subtree = self._subtree(area_scope, practice_area)
            if not subtree:
                return []
            statement = statement.where(Matter.practice_area.in_(sorted(subtree)))
        if matter_kind is not None:
            # The Service facet, same semantics: what the firm is DOING, which is
            # a different question from which law applies, and composes with it.
            subtree = self._subtree(service_scope, matter_kind)
            if not subtree:
                return []
            statement = statement.where(Matter.matter_kind.in_(sorted(subtree)))

        offset = max(0, offset)
        wanted = offset + limit
        visible: list[Matter] = []
        scanned = 0
        batch_size = max(wanted, _VERIFY_BATCH_MIN)
        visible_counts: dict[str, int] = {}
        while len(visible) < wanted:
            batch = self.session.scalars(
                statement.offset(scanned).limit(batch_size)
            ).all()
            if not batch:
                break
            scanned += len(batch)
            # One authorization pass for the whole batch. This used to call
            # citations_for_matter per matter, which built a full citation
            # record for every version of every matter — hundreds of queries and
            # thousands of dicts — only to ask whether any survived. Measured on
            # a 266-matter estate: 36 seconds to return twenty rows of ~190
            # bytes. The counts below answer the same two questions (is anything
            # visible, and how much) at a fixed cost per page.
            counts = self._visible_version_counts([m.id for m in batch], principals)
            for matter in batch:
                count = counts.get(matter.id, 0)
                if not count:
                    continue
                visible_counts[matter.id] = count
                visible.append(matter)
                if len(visible) >= wanted:
                    break
            if len(batch) < batch_size:
                break

        page_matters = visible[offset:]
        matched_names = set(self._group_spellings(practice_group)) if practice_group else set()
        # Paths only, and only when asked for. A caller enumerating a practice
        # otherwise has to open each matter to find out what is in it, and a
        # graded run showed that is where matters get dropped: the agent judged
        # them from search hits and never opened the two whose files decided the
        # answer. One extra query for the whole page, no per-matter round trip,
        # and nothing but the path — a row that also carried titles, types and
        # dates would put a 100-matter page back into megabytes.
        paths_by_matter: dict[str, list[str]] = {}
        if include_documents and page_matters:
            for matter_id, path in self.session.execute(
                select(Document.matter_id, SourceObject.path)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(
                    DocumentVersionSource,
                    DocumentVersionSource.version_id == DocumentVersion.id,
                )
                .join(
                    SourceObject,
                    SourceObject.id == DocumentVersionSource.source_object_id,
                )
                .where(Document.matter_id.in_([m.id for m in page_matters]))
                .order_by(SourceObject.path)
            ).all():
                paths_by_matter.setdefault(matter_id, []).append(path)
        team_by_matter: dict[str, list[dict]] = {}
        for matter_id, person_name, title, group, role in self.session.execute(
            select(
                MatterTeam.matter_id, FirmPerson.name, FirmPerson.title,
                FirmPerson.practice_group, MatterTeam.role,
            )
            .join(FirmPerson, FirmPerson.id == MatterTeam.person_id)
            .where(MatterTeam.matter_id.in_([m.id for m in page_matters] or [""]))
            .order_by(MatterTeam.role, FirmPerson.name)
        ).all():
            team_by_matter.setdefault(matter_id, []).append(
                {"name": person_name, "role": role, "title": title,
                 "practice_group": group}
            )

        result: list[dict] = []
        for matter in page_matters:
            involved = sorted(
                {
                    member["practice_group"]
                    for member in team_by_matter.get(matter.id, [])
                    if member.get("practice_group")
                }
                | ({matter.practice_group} if matter.practice_group else set())
            )
            area_payload = None
            if matter.practice_area:
                area_payload = {
                    "id": matter.practice_area,
                    "label": area_scope.label_of(matter.practice_area)
                    if area_scope
                    else None,
                    "path": area_scope.path_labels(matter.practice_area)
                    if area_scope and matter.practice_area in area_scope.visible
                    else [],
                }
            result.append(
                {
                    "id": matter.id,
                    "project_id": matter.project_id,
                    "project": self.project_reference(matter.project_id),
                    "title": matter.title,
                    "reference_numbers": matter.reference_numbers,
                    "practice_area": area_payload,
                    "matter_kind": {
                        "id": matter.matter_kind,
                        "label": service_scope.label_of(matter.matter_kind)
                        if service_scope
                        else None,
                        # Same shape as practice_area above, and for the same
                        # reason: the leaf label alone hides the hierarchy, so
                        # "Debt Financing Practice" and "Lending Practice" read as
                        # unrelated kinds when they are siblings under "Financing
                        # Practice". A caller scoping a practice needs the path to
                        # see that.
                        "path": service_scope.path_labels(matter.matter_kind)
                        if service_scope and matter.matter_kind in service_scope.visible
                        else [],
                    }
                    if matter.matter_kind
                    else None,
                    # What the matter IS and whether it happened. Both are
                    # properties of the whole folder that no single document
                    # shows, so they are derived by the matter-level pass — and
                    # both change which matters belong in an answer. Without
                    # `lifecycle` a caller cannot tell a closed deal from an
                    # abandoned one without reading every document of every
                    # candidate, which agents did inconsistently and so included
                    # terminated matters in answers about live ones. Without
                    # `instrument` they qualify a matter by what its documents
                    # MENTION rather than what the matter is, so a term loan that
                    # merely repays a revolver counts as a revolver.
                    "lifecycle": matter.lifecycle,
                    # The group that OWNS the matter — the book it is filed in,
                    # which is the group of the partner responsible for it.
                    "practice_group": matter.practice_group,
                    # Who at the firm works it. A caller asking "what has this
                    # partner done" or "who ran this" gets it from the row instead
                    # of reading the matter's intake memo.
                    "firm_team": team_by_matter.get(matter.id, []),
                    # Every group with someone on this matter, the owner included.
                    # A cross-practice financing belongs to more than one book and
                    # a single value hides the others.
                    "practice_groups": involved,
                    # The groups seconded in, owner excluded. A tax partner sitting
                    # on a capital markets IPO makes it a matter the tax group has
                    # WORKED, not one the tax group RUNS, and the difference decides
                    # whether it belongs in an answer about "our tax matters". The
                    # split is stated rather than left to be inferred by comparing
                    # the two fields above: on a filtered page of twenty-six that
                    # inference is where an answer over-includes.
                    "supporting_groups": [
                        group for group in involved if group != matter.practice_group
                    ],
                    "instrument": (matter.profile or {}).get("instrument"),
                    "principal_document": (matter.profile or {}).get(
                        "principal_document"
                    ),
                    "summary": (matter.profile or {}).get("summary"),
                    # A listing is a collection resource: each row carries what a
                    # caller needs to decide which matter to open, plus the COUNT
                    # of citable documents behind it — never the citations
                    # themselves. Embedding them (one citation per visible
                    # version) made a row ~28 KB and a 100-row page ~2.9 MB, and
                    # a partial embed would misrepresent the set. The citations
                    # live where the item does: search_filter / get_document on
                    # the chosen matter.
                    "visible_versions": visible_counts[matter.id],
                }
            )
            if include_documents:
                result[-1]["source_paths"] = paths_by_matter.get(matter.id, [])
            if practice_group is not None:
                # Why THIS row is on a group-filtered page. The filter matches any
                # group staffed on the matter, so a page mixes the group's own book
                # with matters it was merely seconded onto, and a caller answering
                # "which matters does this group have" needs to see which is which
                # without re-deriving it per row.
                result[-1]["group_match"] = (
                    "owner"
                    if matter.practice_group in matched_names
                    else "supporting"
                )
        return result

    def _visible_version_counts(
        self, matter_ids: list[str], principals: set[str]
    ) -> dict[str, int]:
        """Document versions per matter the caller may actually see.

        Same fail-closed rule as building a citation for each version and
        counting what came back — a version counts only when the caller passes
        the ACL predicate AND holds at least one authorized source observation
        for it — but as one set-based pass over the whole batch instead of a
        citation record per version.
        """
        wanted = [m for m in matter_ids if m]
        if not wanted:
            return {}
        rows = self.session.execute(
            select(Document.matter_id, DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.matter_id.in_(wanted))
        ).all()
        if not rows:
            return {}
        authorized = self._bulk_authorized_sources(
            [version_id for _, version_id in rows], principals
        )
        counts: dict[str, int] = {}
        for matter_id, version_id in rows:
            if authorized.get(version_id):
                counts[matter_id] = counts.get(matter_id, 0) + 1
        return counts

    def list_matters_page(
        self,
        *,
        principals: set[str],
        limit: int = 100,
        offset: int = 0,
        practice_area: str | None = None,
        matter_kind: str | None = None,
        lifecycle: str | None = None,
        practice_group: str | None = None,
        firm_person: str | None = None,
        include_documents: bool = False,
    ) -> Page:
        """``list_matters`` plus an exact ``has_more``, via one extra matter.

        No ``total``: counting visible matters means running the per-matter
        authorization check over every matter in the estate, which is the cost of
        every page at once. ``has_more`` answers the question a caller actually
        has, for the price of one extra row.
        """
        return Page.probe(
            self.list_matters(
                principals=principals,
                limit=limit + 1,
                offset=offset,
                practice_area=practice_area,
                matter_kind=matter_kind,
                lifecycle=lifecycle,
                practice_group=practice_group,
                firm_person=firm_person,
                include_documents=include_documents,
            ),
            offset=offset,
            limit=limit,
        )

    def list_matter_documents(
        self, matter_id: str, *, principals: set[str]
    ) -> list[dict]:
        """Every document in one matter, complete, in folder order.

        A matter is a bounded set — tens of documents, occasionally a couple of
        hundred — so this is the one listing that does not paginate. That is the
        point of it. `search_filter(matter_id=...)` answers the same question a
        page at a time, and an agent that asks once and reads twenty rows of a
        seventy-five-document matter concludes the other fifty-five are not
        there: on the last benchmark run one reported "no LPA exists in that
        matter's file" while `Amended Governing Documents/lpa-amendment-no-1.docx`
        sat in the folder, executed, titled "Amendment No. 1 to the Amended and
        Restated Agreement of Limited Partnership". Seventeen graded criteria
        were lost to that shape of mistake, every one of them a document the
        caller could have reached.

        It is deliberately the same view the matter-profile pass gets, which is
        why that pass can judge a folder no single document reveals: path, title,
        type, date, version standing. No excerpts and no citations — this says
        what the matter CONTAINS, and get_document says what a document says.
        """
        rows = self.session.execute(
            select(
                SourceObject.path,
                Document.id,
                Document.title,
                Document.doc_type,
                Document.doc_date,
                DocumentVersion.id,
                DocumentVersion.status,
            )
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObject.id,
            )
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentVersionSource.version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.matter_id == matter_id)
            .order_by(SourceObject.path)
        ).all()
        if not rows:
            return []
        authorized = self._bulk_authorized_sources(
            [version_id for *_, version_id, _ in rows], principals
        )
        try:
            type_scope = self.config.ontology_facet("document_type")
        except ValueError:
            type_scope = None
        out: list[dict] = []
        for path, doc_id, title, doc_type, doc_date, version_id, status in rows:
            if not authorized.get(version_id):
                continue
            out.append(
                {
                    "source_path": path,
                    "document_id": doc_id,
                    "version_id": version_id,
                    "title": title,
                    "doc_type_label": (
                        type_scope.label_of(doc_type)
                        if type_scope and doc_type in type_scope.visible
                        else None
                    ),
                    "doc_date": doc_date.isoformat() if doc_date else None,
                    "version_status": status,
                }
            )
        return out

    def list_firm_people(
        self,
        *,
        principals: set[str],
        limit: int = 100,
        offset: int = 0,
        practice_group: str | None = None,
        name: str | None = None,
    ) -> Page:
        """The firm's own lawyers, with the group they sit in and how many
        visible matters each works.

        This is the directory behind the ``firm_person`` and ``practice_group``
        filters on ``list_matters``. Without it a caller has to already know how
        a name is spelled before it can filter by one, and a filter you can only
        use when you know the answer is not a filter.

        A person is listed only when the caller can see at least one matter they
        are on, and ``matter_count`` counts only those — so the directory never
        leaks the shape of an estate the caller has no access to.
        """
        if limit <= 0:
            return Page(items=[], offset=max(0, offset), limit=0, has_more=False)
        statement = select(FirmPerson).order_by(FirmPerson.name, FirmPerson.id)
        if practice_group is not None:
            names = self._group_spellings(practice_group)
            if not names:
                return Page(items=[], offset=max(0, offset), limit=limit, has_more=False)
            statement = statement.where(FirmPerson.practice_group.in_(names))
        if name is not None:
            statement = statement.where(self._person_name_matches(name))
        people = self.session.scalars(statement).all()
        if not people:
            return Page(items=[], offset=max(0, offset), limit=limit, has_more=False)

        assignments = self.session.execute(
            select(MatterTeam.person_id, MatterTeam.matter_id, MatterTeam.role).where(
                MatterTeam.person_id.in_([p.id for p in people])
            )
        ).all()
        # One authorization pass over every matter in the directory, rather than
        # per person: the same matter is staffed by several people, and asking
        # once per person would re-check it once per seat.
        visible = self._visible_version_counts(
            sorted({matter_id for _, matter_id, _ in assignments}), principals
        )
        matters_by_person: dict[str, set[str]] = {}
        roles_by_person: dict[str, set[str]] = {}
        for person_id, matter_id, role in assignments:
            if not visible.get(matter_id):
                continue
            matters_by_person.setdefault(person_id, set()).add(matter_id)
            if role:
                roles_by_person.setdefault(person_id, set()).add(role)

        rows = [
            {
                "id": person.id,
                "name": person.name,
                "title": person.title,
                "practice_group": person.practice_group,
                "email": person.email,
                "roles": sorted(roles_by_person.get(person.id, ())),
                # Collection row: the count, not the matters. Open them with
                # list_matters(firm_person=...), which pages and carries the
                # metadata a caller needs to choose among them.
                "matter_count": len(matters_by_person.get(person.id, ())),
            }
            for person in people
            if matters_by_person.get(person.id)
        ]
        # The whole directory is materialized and authorized above, so `total`
        # here is the real number of people the caller can see, not a bound.
        return Page.slice(rows, offset=max(0, offset), limit=limit)

    # How alike a typed name and a stored one must be before a fuzzy fallback
    # accepts it. Both are trigram similarities on the normalized form, so they
    # measure spelling, not meaning. 0.45 admits ordinary typos and a dropped
    # letter; below that the near-misses start being different people.
    _NAME_FUZZ = 0.45
    _GROUP_FUZZ = 0.55

    def _group_spellings(self, practice_group: str) -> list[str]:
        """Every stored spelling of the group the caller means.

        A caller types the group the way they say it — "Banking and Finance",
        "the Banking & Finance Group", "Private Funds" — and the estate stores it
        the way its documents wrote it. Matching those two strings directly is
        what made the filter silently return nothing: a group that exists, a
        caller who named it, and an empty page that reads exactly like "we have
        no such matters".

        So the typed name is resolved the same way an insertion resolves one:
        normalize, then look for a group whose canonical name or recorded alias
        matches. Aliases are the important half — they hold the equivalences a
        model already judged at insertion time ("Private Funds" is Funds & Asset
        Management), and re-deciding that at query time would be both slower and
        free to disagree with the stored data.

        Falls back to trigram similarity for a name close to a real group but not
        equal to any spelling of it, and finally to the typed string itself, so a
        group recorded before the registry existed is still reachable.
        """
        normalized = normalize_group(practice_group)
        if not normalized:
            return []
        key = normalize_entity_name(normalized)
        groups = self.session.scalars(select(FirmPracticeGroup)).all()
        wanted = {
            group.name
            for group in groups
            if group.normalized_name == key or key in (group.aliases or [])
        }
        if not wanted and groups:
            best = max(
                (
                    (name_similarity(key, group.normalized_name)[0], group.name)
                    for group in groups
                ),
                default=(0.0, ""),
            )
            if best[0] >= self._GROUP_FUZZ:
                wanted = {best[1]}
        # Whatever the registry knows, the raw spellings still count: a person or
        # matter written before this table existed holds a string no group row
        # claims, and dropping it here would hide them.
        stored = self.session.scalars(
            select(FirmPerson.practice_group)
            .where(FirmPerson.practice_group.isnot(None))
            .union(
                select(Matter.practice_group).where(Matter.practice_group.isnot(None))
            )
        ).all()
        wanted |= {
            spelling
            for spelling in stored
            if normalize_entity_name(normalize_group(spelling) or "") == key
        }
        return sorted(wanted)

    def _person_name_matches(self, query: str):
        """Predicate for "this is the lawyer they meant".

        Names arrive in every order a person writes one: "Sylvia Hartwell",
        "Hartwell, Sylvia", "Hartwell", "S. Hartwell", "Sylvia J. Hartwell". A
        substring test on the normalized name answered only the first, third and
        an accidental fourth — reversing the tokens or adding a middle initial
        returned nobody, and nobody is indistinguishable from "that lawyer has no
        matters".

        Resolved in three passes, each tried only when the one before it found
        nothing, so a precise query never widens:

        1. every token present, in any order, matching at a word start. An
           initial finds the name it abbreviates, so "L. Cross" reaches Leonard
           and not Pamela.
        2. the same with single-letter tokens dropped. This is for the initial
           the caller supplies and the estate does not store — "Sylvia J.
           Hartwell" against a recorded "Sylvia Hartwell" — and it only runs when
           insisting on the initial found no one at all.
        3. trigram similarity, for a genuine misspelling where no token matches.

        Two lawyers who share a surname stay two lawyers: "Cross" returns both
        because it names both, and "Leonard Cross" returns one.
        """
        normalized = normalize_entity_name(query)
        tokens = normalized.split()
        if not tokens:
            return false()

        def all_of(wanted: list[str]):
            return and_(
                *(
                    or_(
                        FirmPerson.normalized_name.like(f"{token}%"),
                        FirmPerson.normalized_name.like(f"% {token}%"),
                    )
                    for token in wanted
                )
            )

        attempts = [all_of(tokens)]
        longer = [token for token in tokens if len(token) > 1]
        if longer and len(longer) != len(tokens):
            attempts.append(all_of(longer))
        attempts.append(
            func.similarity(FirmPerson.normalized_name, normalized) >= self._NAME_FUZZ
        )
        for predicate in attempts:
            ids = self.session.scalars(
                select(FirmPerson.id).where(predicate)
            ).all()
            if ids:
                return FirmPerson.id.in_(ids)
        return false()

    def _subtree(self, scope, node_id: str) -> set[str]:
        """Every visible node id in ``scope`` at or below ``node_id``."""
        if scope is None:
            return set()
        return {
            candidate
            for candidate in scope.visible
            if node_id in scope.ancestors(candidate)
        }

    def search_decisions(
        self, query: str, *, principals: set[str], limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """Anonymized drafting rationale, ranked lexically and ACL-filtered.

        Every authorized record is scored before the page is cut, so an offset
        page is exact and ``search_decisions_page`` can report a real total.
        """
        return Page.slice(
            self._scored_decisions(query, principals),
            offset=max(0, offset),
            limit=limit,
        ).items

    def search_decisions_page(
        self, query: str, *, principals: set[str], limit: int = 20, offset: int = 0
    ) -> Page:
        """``search_decisions`` with an exact total.

        Every authorized record has to be scored to rank any of them, so the size
        of the result set is already known by the time the page is cut — unlike
        the index-backed searches, this one can say how many matches exist.
        """
        return Page.slice(
            self._scored_decisions(query, principals),
            offset=max(0, offset),
            limit=limit,
        )

    def _scored_decisions(self, query: str, principals: set[str]) -> list[dict]:
        """Every authorized decision record matching ``query``, best first."""
        terms = _terms(query)
        rows = self.session.scalars(select(DecisionRecord)).all()
        scored: list[tuple[float, DecisionRecord, dict]] = []
        for row in rows:
            if row.document_id is None:
                continue
            document = self.session.get(Document, row.document_id)
            if document is None:
                continue
            evidence_version_id = row.version_to or document.latest_final_version_id
            if not evidence_version_id:
                continue
            version = self.session.get(DocumentVersion, evidence_version_id)
            if version is None or version.document_id != document.id:
                continue
            authorized_sources = self._authorized_sources(version.id, principals)
            evidence_source_ids = {
                str(item.get("source_object_id"))
                for item in (row.source_evidence or [])
                if isinstance(item, dict) and item.get("source_object_id")
            }
            if evidence_source_ids:
                authorized_sources = [
                    source for source in authorized_sources if source.id in evidence_source_ids
                ]
            if not authorized_sources:
                continue
            citation = self._citation(document, version, authorized_sources)
            haystack = f"{row.locus or ''} {row.change_summary or ''} {row.rationale_text}"
            score = _lexical_score(terms, _terms(haystack))
            if not terms or score > 0:
                scored.append((score, row, citation))
        # Record id breaks score ties, so two calls that page through the same
        # result set cut it at the same places.
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [
            {
                "id": row.id,
                "document_id": row.document_id,
                "project_id": citation["project"]["id"] if citation["project"] else None,
                "version_from": row.version_from,
                "version_to": row.version_to,
                "locus": row.locus,
                "change_summary": row.change_summary,
                "rationale_category": row.rationale_category,
                "rationale_text": row.rationale_text,
                "generalizable": row.generalizable,
                "score": round(score, 6),
                # Collection row: the decision content plus document_id to open
                # the underlying document. The citation gated visibility above;
                # its record comes from get_document, not from every list row.
            }
            for score, row, citation in scored
        ]

    def project_reference(self, project_id: str | None) -> dict | None:
        """Return the stable, non-secret identity of a project."""
        project = self.session.get(Project, project_id) if project_id else None
        if project is None:
            return None
        return {"id": project.id, "key": project.key, "name": project.name}

    def citations_for_document(self, document_id: str, principals: set[str]) -> list[dict]:
        versions = self.session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.ordinal.desc().nullslast(), DocumentVersion.id)
        ).all()
        citations = [
            citation
            for version in versions
            if (citation := self.citation_for_version(version.id, principals)) is not None
        ]
        return _dedupe_citations(citations)

    def citation_for_version(
        self,
        version_id: str,
        principals: set[str],
        *,
        source_object_ids: set[str] | None = None,
        matched_chunk: dict | None = None,
    ) -> dict | None:
        version = self.session.get(DocumentVersion, version_id)
        if version is None:
            return None
        document = self.session.get(Document, version.document_id)
        if document is None:
            return None
        sources = self._authorized_sources(version.id, principals)
        if source_object_ids is not None:
            sources = [source for source in sources if source.id in source_object_ids]
        if not sources:
            return None
        return self._citation(document, version, sources, matched_chunk=matched_chunk)

    def citations_for_matter(self, matter_id: str, principals: set[str]) -> list[dict]:
        version_ids = self.session.scalars(
            select(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.matter_id == matter_id)
            .order_by(Document.id, DocumentVersion.ordinal.desc().nullslast())
        ).all()
        citations = [
            citation
            for version_id in version_ids
            if (citation := self.citation_for_version(version_id, principals)) is not None
        ]
        return _dedupe_citations(citations)

    def citations_for_source_object(
        self, source_object_id: str | None, principals: set[str]
    ) -> list[dict]:
        if not source_object_id:
            return []
        version_ids = self.session.scalars(
            select(DocumentVersionSource.version_id).where(
                DocumentVersionSource.source_object_id == source_object_id
            )
        ).all()
        citations = [
            citation
            for version_id in version_ids
            if (
                citation := self.citation_for_version(
                    version_id,
                    principals,
                    source_object_ids={source_object_id},
                )
            )
            is not None
        ]
        return _dedupe_citations(citations)

    def citations_for_party_or_client(
        self, entity_type: str, entity_id: str, principals: set[str]
    ) -> list[dict]:
        if entity_type == "client":
            matter_ids = self.session.scalars(
                select(MatterClient.matter_id).where(MatterClient.client_id == entity_id)
            ).all()
        elif entity_type == "party":
            matter_ids = self.session.scalars(
                select(MatterParty.matter_id).where(MatterParty.party_id == entity_id)
            ).all()
        else:
            return []
        return _dedupe_citations(
            [
                citation
                for matter_id in matter_ids
                for citation in self.citations_for_matter(matter_id, principals)
            ]
        )

    def citations_for_reference(
        self, entity_type: str, entity_id: str, principals: set[str]
    ) -> list[dict]:
        """Resolve a graph entity to the exact authorized document observations behind it."""
        if entity_type == "document_version":
            citation = self.citation_for_version(entity_id, principals)
            return [citation] if citation else []
        if entity_type == "document":
            return self.citations_for_document(entity_id, principals)
        if entity_type == "matter":
            return self.citations_for_matter(entity_id, principals)
        if entity_type == "thread":
            document_ids = self.session.scalars(
                select(Relation.from_id).where(
                    Relation.kind == "belongs_to_thread",
                    Relation.from_type == "document",
                    Relation.to_type == "thread",
                    Relation.to_id == entity_id,
                )
            ).all()
            return _dedupe_citations(
                [
                    citation
                    for document_id in document_ids
                    for citation in self.citations_for_document(document_id, principals)
                ]
            )
        if entity_type == "decision_record":
            record = self.session.get(DecisionRecord, entity_id)
            if record is None:
                return []
            version_id = record.version_to
            if not version_id and record.document_id:
                document = self.session.get(Document, record.document_id)
                version_id = document.latest_final_version_id if document else None
            evidence_source_ids = {
                str(item.get("source_object_id"))
                for item in (record.source_evidence or [])
                if isinstance(item, dict) and item.get("source_object_id")
            }
            citation = (
                self.citation_for_version(
                    version_id,
                    principals,
                    source_object_ids=evidence_source_ids or None,
                )
                if version_id
                else None
            )
            return [citation] if citation else []
        if entity_type == "eval_record":
            record = self.session.get(EvalRecord, entity_id)
            citation = (
                self.citation_for_version(record.reference_output_ref, principals)
                if record and record.reference_output_ref
                else None
            )
            return [citation] if citation else []
        return []

    def _search(
        self,
        *,
        query: str | None,
        principals: set[str],
        filters: SearchFilters,
        limit: int,
        offset: int = 0,
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if limit < 0:
            raise ValueError("limit must be non-negative")
        # +1 because the paged callers ask for one extra hit to decide has_more;
        # a caller sitting exactly on the cap must not be refused for the probe.
        if offset + limit > _MAX_RANKED_WINDOW + 1:
            raise ValueError(
                f"offset + limit must not exceed {_MAX_RANKED_WINDOW} for ranked search "
                "(every page re-ranks the whole window). Narrow the search with "
                "matter_id, doc_type, party, or a date range instead of paging deeper."
            )
        # Query embedding and the optional rerank are both spend on the read path;
        # they belong to "search", not to whichever ingestion stage ran last.
        with usage_stage("search"):
            return self._search_opensearch(
                query=query,
                principals=principals,
                filters=filters,
                limit=limit,
                offset=offset,
                scope=scope,
            )

    def _drop_superseded(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Apply ``only_final``: keep the authoritative version of each document.

        A version is dropped only when the SAME document has a final/executed
        version and this is not it. A document whose only version is a draft is
        kept — nothing supersedes it, and hiding it made whole matters look
        smaller than they are (12 documents returning 10) for no reason a caller
        could see.
        """
        if not hits:
            return hits
        # One query for the whole page: latest_final_version_id is a cache and is
        # not always populated, so authority is read from the versions themselves.
        document_ids = {hit.document_id for hit in hits}
        rows = self.session.execute(
            select(DocumentVersion.document_id, DocumentVersion.id, DocumentVersion.status)
            .where(DocumentVersion.document_id.in_(document_ids))
        ).all()
        authoritative: dict[str, set[str]] = {}
        for document_id, version_id, status in rows:
            if status in ("final", "executed"):
                authoritative.setdefault(document_id, set()).add(version_id)
        return [
            hit
            for hit in hits
            if hit.document_id not in authoritative
            or hit.version_id in authoritative[hit.document_id]
        ]

    def _select_document_version(
        self, document_id: str, version_id: str | None
    ) -> tuple[Document, DocumentVersion] | None:
        document = self.session.get(Document, document_id)
        if document is None:
            return None
        selected_version_id = version_id or document.latest_final_version_id
        if selected_version_id:
            version = self.session.get(DocumentVersion, selected_version_id)
        else:
            version = self.session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.ordinal.desc())
            ).first()
        if version is None or version.document_id != document.id:
            return None
        return document, version

    def _document_summary(
        self, document: Document, principals: set[str]
    ) -> dict | None:
        selected = self._select_document_version(document.id, None)
        if selected is None:
            return None
        _, version = selected
        citation = self.citation_for_version(version.id, principals)
        if citation is None:
            return None
        # A graph/listing summary is a collection row: identity and the info
        # needed to decide whether to open the document — not its citation
        # record. The citation still gates visibility above (no citation, no
        # row); the record itself comes from get_document on the chosen id.
        return {
            "document_id": document.id,
            "version_id": version.id,
            "project_id": document.project_id,
            "matter_id": document.matter_id,
            "title": document.title,
            "doc_type": document.doc_type,
            "version_status": version.status,
            "source_paths": [
                source["path"] for source in citation["source_objects"]
            ],
        }

    def _resolve_matter_filters(self, filters: SearchFilters) -> SearchFilters:
        """Translate the matter-level filters into the matter-id set they cover.

        A practice group, a lawyer and a lifecycle are properties of the MATTER, and
        the index stores chunks. Rather than denormalising three mutable fields onto
        every chunk of every document — where they would go stale the moment a
        partner moves groups — they are resolved here against the same predicates
        ``list_matters`` uses, and the backend is handed a matter-id set.

        That shared predicate is the point: a caller who lists a group's matters and
        then searches within that group must be looking at the same set, and would
        have no way to notice if they were not.

        An empty match is preserved rather than dropped, so a filter that covers no
        matter returns nothing instead of silently widening to the whole estate.
        """
        wanted = (filters.practice_group, filters.firm_person, filters.lifecycle)
        if not any(wanted):
            return filters
        statement = select(Matter.id)
        if filters.lifecycle:
            statement = statement.where(Matter.lifecycle == filters.lifecycle)
        if filters.practice_group:
            names = self._group_spellings(filters.practice_group)
            if not names:
                return replace(
                    filters, practice_group=None, firm_person=None, lifecycle=None,
                    matter_ids=[],
                )
            statement = statement.where(
                or_(
                    Matter.practice_group.in_(names),
                    Matter.id.in_(
                        select(MatterTeam.matter_id)
                        .join(FirmPerson, FirmPerson.id == MatterTeam.person_id)
                        .where(FirmPerson.practice_group.in_(names))
                    ),
                )
            )
        if filters.firm_person:
            statement = statement.where(
                Matter.id.in_(
                    select(MatterTeam.matter_id)
                    .join(FirmPerson, FirmPerson.id == MatterTeam.person_id)
                    .where(self._person_name_matches(filters.firm_person))
                )
            )
        matched = list(self.session.scalars(statement).all())
        if filters.matter_ids is not None:
            allowed = set(filters.matter_ids)
            matched = [matter_id for matter_id in matched if matter_id in allowed]
        return replace(
            filters, practice_group=None, firm_person=None, lifecycle=None,
            matter_ids=matched,
        )

    def _resolve_practice_area(self, filters: SearchFilters) -> SearchFilters:
        """Translate the ontology filters into the matter-id set they cover.

        practice_area is an Area-of-Law node and matter_kind a Service node — what
        body of law applies, versus what the firm is DOING. Both live on Matter
        rather than on a chunk, both use SUBTREE semantics (a parent covers its
        children), and both compose, so "fund formations in healthcare" is one
        call. They intersect: each pass narrows the matter set the next one sees.

        practice_area lives on Matter, not on the chunk, so it cannot be a term on the
        index. We resolve it here with the same SUBTREE semantics as list_matters (a
        parent area matches its children) and hand the backend a matter-id filter. The
        backend's ACL scope still applies on top, so this set only narrows results — it is
        not an authorization boundary. An empty match is preserved, not dropped: a
        A filter that covers no matter yields no hits rather than silently widening
        to every matter.
        """
        if not filters.practice_area and not filters.matter_kind:
            # Untouched, and the SAME object: callers rely on a no-op resolver
            # being free, and returning a copy would quietly break that.
            return filters
        for wanted, facet, column in (
            (filters.practice_area, "area_of_law", Matter.practice_area),
            (filters.matter_kind, "service", Matter.matter_kind),
        ):
            if not wanted:
                continue
            try:
                scope = self.config.ontology_facet(facet)
            except ValueError:
                scope = None
            matched: list[str] = []
            if scope is not None:
                rows = self.session.execute(
                    select(Matter.id, column).where(column.isnot(None))
                ).all()
                matched = [
                    matter_id
                    for matter_id, node in rows
                    if wanted in scope.ancestors(node)
                ]
            if filters.matter_ids is not None:
                allowed = set(filters.matter_ids)
                matched = [matter_id for matter_id in matched if matter_id in allowed]
            filters = replace(filters, matter_ids=matched)
        return replace(filters, practice_area=None, matter_kind=None)

    def _search_opensearch(
        self,
        *,
        query: str | None,
        principals: set[str],
        filters: SearchFilters,
        limit: int,
        offset: int = 0,
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        """Fuse ACL-scoped ranked legs, collapse version sprawl, re-verify in SQL.

        Fusion never sees an unauthorized row (every leg runs inside the compiled
        scope), the SQL re-verify is the authoritative backstop, and legal authority
        decays by version status (supersession), not by age.

        ``scope`` lets a caller that already compiled the access scope (the API
        layer reports it in its response envelope) hand it in instead of paying the
        compile twice per request. It must have been compiled for the same
        principals and the same project filter."""

        from knowledge_index.search_backend import OpenSearchIndex

        filters = self._resolve_matter_filters(filters)
        # practice_area is a Matter attribute, not a chunk field; translate it into a
        # matter-id set the backend can filter on (SUBTREE semantics, same as list_matters).
        filters = self._resolve_practice_area(filters)

        retrieval = self.config.retrieval
        # Config-level chunk-kind scope (benchmark ablations ride on this); an explicit
        # per-request kind filter wins.
        if retrieval.search_chunk_kinds and not filters.chunk_kind and not filters.chunk_kinds:
            filters = replace(filters, chunk_kinds=list(retrieval.search_chunk_kinds))
        if scope is None:
            scope = AccessService(self.session).compile_scope(
                principals,
                project_ids=[filters.project_id] if filters.project_id else [],
            )
        index = OpenSearchIndex(self.config)

        # Everything below ranks and authorizes the whole window (the page the
        # caller asked for plus everything ahead of it) and slices at the end.
        # An offset cannot be pushed into the index: the rows the index skips are
        # not the rows the caller has already seen, because collapse and the ACL
        # re-verify drop rows after the index has ranked them.
        window = offset + limit

        # Metadata-only search: no query, so no legs to fuse — deterministic order.
        if not query or not query.strip():
            rows = index.search(
                query_vector=None, scope=scope, filters=filters, limit=window
            )
            hits = self._materialize_metadata(rows, principals=principals, limit=window)
            if filters.only_final:
                hits = self._drop_superseded(hits)
            return hits[offset:]

        query_terms = _terms(query)
        query_cf = query.casefold()

        # Three ACL-scoped ranked legs fused by RRF. The identifier leg matches the
        # query text against the model-extracted identifiers indexed per document — no
        # regex parsing of the query; a pasted case number simply matches its document.
        # All three legs run in a single `_msearch` round-trip (query embedding is the
        # only synchronous model call on the hot path).
        # Fetch a deeper candidate pool than the caller asked for: fusion, the
        # version-status boost, collapse and rerank all reorder, and a pool the size
        # of the answer can only ever surface what a single leg already ranked in its
        # own top-`limit`. The ranking stages need room to work.
        pool = max(window * retrieval.candidate_pool_factor, window)
        # A zero-weight leg is a disabled leg: it must not be embedded, queried, or
        # allowed to contribute candidates. It previously still filled result slots
        # at score 0.0, so "lexical off" quietly returned lexical hits ranked last.
        want_semantic = retrieval.weight_semantic > 0
        leg_hits = index.multi_search(
            query_text=query,
            query_vector=embed_text(query, self.config) if want_semantic else None,
            scope=scope,
            filters=filters,
            limit=pool,
        )
        legs: list[tuple[float, list[dict]]] = [
            (retrieval.weight_lexical, leg_hits["lexical"]),
            (retrieval.weight_semantic, leg_hits["semantic"]),
            (retrieval.weight_identifier, leg_hits["identifier"]),
        ]

        # RRF fuse: score(chunk) += weight_leg / (k + rank), aggregated by chunk id.
        candidates: dict[str, _Candidate] = {}
        for leg_index, (weight, rows) in enumerate(legs):
            if weight <= 0:
                continue
            is_identifier_leg = leg_index == len(legs) - 1
            for rank, row in enumerate(rows):
                source = row.get("_source") or {}
                chunk_id = row.get("_id")
                if not chunk_id:
                    continue
                candidate = candidates.get(chunk_id)
                if candidate is None:
                    candidate = _Candidate(chunk_id=chunk_id, source=source)
                    candidates[chunk_id] = candidate
                candidate.fused_score += weight / (retrieval.fusion_rrf_k + rank)
                if is_identifier_leg:
                    # which of this document's identifiers the query names (substring,
                    # not regex): powers the SearchHit.matched_identifiers field
                    candidate.matched_identifiers = sorted(
                        {
                            str(item)
                            for item in (source.get("identifiers") or [])
                            if str(item).strip() and str(item).casefold() in query_cf
                        }
                    )

        # Version-status boost: supersession decay, not age decay.
        for candidate in candidates.values():
            status = str(candidate.source.get("version_status") or "unknown")
            candidate.fused_score *= retrieval.version_status_boost.get(status, 1.0)

        hits = self._collapse_and_verify(
            candidates.values(),
            principals=principals,
            query_terms=query_terms,
            collapse=retrieval.collapse_per_document,
            max_per_document=retrieval.max_chunks_per_document,
            # _rerank reorders the fused top-`_RERANK_WINDOW`, so with rerank on
            # that prefix must be exact, not just the top `window`.
            needed=max(window, _RERANK_WINDOW) if retrieval.rerank_enabled else window,
        )
        hits.sort(key=lambda item: item.score, reverse=True)

        if retrieval.rerank_enabled:
            hits = self._rerank(query, hits)
        if filters.only_final:
            hits = self._drop_superseded(hits)
        return hits[offset:window]

    def _materialize_metadata(
        self, rows: list[dict], *, principals: set[str], limit: int
    ) -> list[SearchHit]:
        """First authorized hit per version, in index order, verified in batches.

        Rows are authorized lazily front-to-back: the common case fills ``limit``
        from the first batch, and a page whose head is unauthorized keeps reading
        until the page is full or the rows run out — same results as verifying
        everything, without paying for rows that were never going to be shown."""
        best_by_version: dict[str, SearchHit] = {}
        batch_size = max(limit, _VERIFY_BATCH_MIN)
        position = 0
        while position < len(rows) and len(best_by_version) < limit:
            batch = rows[position : position + batch_size]
            position += len(batch)
            sources = [row.get("_source") or {} for row in batch]
            # All three maps stay referenced for the whole batch (the projects
            # one only as a keep-alive for citation building) — see
            # _warm_identity_map for why dropping them would resurrect the N+1.
            documents, versions, _projects = self._warm_identity_map(sources)
            authorized = self._bulk_authorized_sources(
                (
                    source.get("document_version_id")
                    for source in sources
                    if source.get("document_version_id") in versions
                ),
                principals,
            )
            for row, source in zip(batch, sources, strict=True):
                if source.get("document_id") not in documents:
                    continue  # stale index row: the document is gone from SQL
                visible = authorized.get(source.get("document_version_id") or "", [])
                if not visible:
                    continue
                hit = self._hit_from_source(
                    source,
                    query_terms=set(),
                    score=0.0,
                    authorized_sources=visible,
                    chunk_id=row.get("_id"),
                )
                if hit is None or hit.version_id in best_by_version:
                    continue
                best_by_version[hit.version_id] = hit
                if len(best_by_version) >= limit:
                    break
        return list(best_by_version.values())[:limit]

    def _collapse_and_verify(
        self,
        candidates,
        *,
        principals: set[str],
        query_terms: set[str],
        collapse: bool,
        max_per_document: int,
        needed: int,
    ) -> list[SearchHit]:
        """Rank first, authorize lazily, and always in bulk.

        Candidates are grouped per document in fused-score order and authorized
        batch-wise through set-based queries (:meth:`_bulk_authorized_sources`)
        instead of per-candidate round-trips — the difference between ~460 and a
        handful of SQL statements per search. Processing stops once the top
        ``needed`` hits are exact: a group's provisional score (its best chunk)
        bounds its final score from above, so when ``needed`` verified hits all
        score at least the next group's provisional score, no unprocessed group
        can displace them. Laziness changes how much work is done, never what
        survives — unauthorized rows are still dropped before anything is
        returned, and the SQL check remains the authoritative backstop."""
        ordered = sorted(candidates, key=lambda item: item.fused_score, reverse=True)
        groups: dict[str, list[_Candidate]] = {}
        for candidate in ordered:
            document_id = candidate.source.get("document_id")
            if not document_id or not candidate.source.get("document_version_id"):
                continue
            groups.setdefault(document_id, []).append(candidate)
        # Each key is inserted at its best-scoring candidate, so dict order is
        # already descending provisional score; every group inherits the global
        # candidate order, so group[0] is that document's best chunk.
        ordered_groups = list(groups.items())

        results: list[SearchHit] = []
        batch_size = max(needed, _VERIFY_BATCH_MIN)
        position = 0
        while position < len(ordered_groups):
            if len(results) >= needed:
                floor = sorted((hit.score for hit in results), reverse=True)[needed - 1]
                if ordered_groups[position][1][0].fused_score <= floor:
                    break
            batch = ordered_groups[position : position + batch_size]
            position += len(batch)
            batch_candidates = [candidate for _, group in batch for candidate in group]
            # All three maps stay referenced for the whole batch (the projects
            # one only as a keep-alive for citation building) — see
            # _warm_identity_map for why dropping them would resurrect the N+1.
            documents, versions, _projects = self._warm_identity_map(
                [candidate.source for candidate in batch_candidates]
            )
            authorized = self._bulk_authorized_sources(
                (
                    candidate.source.get("document_version_id")
                    for candidate in batch_candidates
                    if candidate.source.get("document_version_id") in versions
                ),
                principals,
            )
            for document_id, group in batch:
                if document_id not in documents:
                    continue  # stale index row: the document is gone from SQL
                hits: list[SearchHit] = []
                for candidate in group:
                    visible = authorized.get(
                        candidate.source.get("document_version_id") or "", []
                    )
                    if not visible:
                        continue
                    hit = self._hit_from_source(
                        candidate.source,
                        query_terms=query_terms,
                        score=candidate.fused_score,
                        authorized_sources=visible,
                        chunk_id=candidate.chunk_id,
                        matched_identifiers=candidate.matched_identifiers,
                    )
                    if hit is not None:
                        hits.append(hit)
                if not hits:
                    continue
                if collapse:
                    # Supersession is a WITHIN-CHAIN concept — "which version of this
                    # document do I show" — so it is decided here, among versions of
                    # one document, and not by scaling the cross-document score
                    # (which demoted a relevant draft below an irrelevant executed
                    # document elsewhere in the corpus; see version_status_boost).
                    latest_final = self._latest_final_version_id(document_id)
                    hits.sort(
                        key=lambda item: (
                            item.score,
                            item.version_id == latest_final,
                            _VERSION_STATUS_ORDER.get(item.version_status, 0),
                        ),
                        reverse=True,
                    )
                    results.append(hits[0])
                else:
                    hits.sort(key=lambda item: item.score, reverse=True)
                    results.extend(hits[:max_per_document])
        return results

    def _hit_from_source(
        self,
        source: dict,
        *,
        query_terms: set[str],
        score: float,
        authorized_sources: list[SourceObject],
        chunk_id: str | None = None,
        matched_identifiers: list[str] | None = None,
    ) -> SearchHit | None:
        """Materialize one index row into a SearchHit.

        Authorization happened before this point: ``authorized_sources`` is the
        caller-supplied output of :meth:`_bulk_authorized_sources` for this row's
        version, and an empty list keeps the row fail-closed."""
        version_id = source.get("document_version_id")
        document_id = source.get("document_id")
        if not version_id or not document_id:
            return None
        document = self.session.get(Document, document_id)
        version = self.session.get(DocumentVersion, version_id)
        if document is None or version is None or version.document_id != document.id:
            return None
        if not authorized_sources:
            return None
        meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
        indexed_source_object_id = meta.get("source_object_id")
        visible_source_ids = {item.id for item in authorized_sources}
        matched_chunk = (
            {
                "id": chunk_id,
                "source_object_id": (
                    indexed_source_object_id
                    if indexed_source_object_id in visible_source_ids
                    else None
                ),
                "kind": meta.get("kind"),
                "locus": meta.get("locus"),
            }
            if chunk_id
            else None
        )
        citation = self._citation(
            document,
            version,
            authorized_sources,
            matched_chunk=matched_chunk,
        )
        doc_type_label = None
        if document.doc_type:
            try:
                doc_type_label = self.config.doc_ontology().label_of(document.doc_type)
            except ValueError:
                doc_type_label = None  # artifact unavailable: id still returned
        return SearchHit(
            project_id=document.project_id,
            document_id=document.id,
            version_id=version.id,
            matter_id=document.matter_id,
            title=document.title,
            doc_type=document.doc_type,
            doc_type_label=doc_type_label,
            doc_date=document.doc_date.isoformat() if document.doc_date else None,
            language=document.language,
            matter_ref=(
                matter.reference_numbers[0]
                if (matter := self._warm_matters.get(document.matter_id or ""))
                and matter.reference_numbers
                else None
            ),
            parties=[
                {
                    "name": party.name,
                    "role": entry.get("role_in_doc"),
                }
                for entry in (document.parties or [])
                if isinstance(entry, dict)
                and (party := self._warm_parties.get(str(entry.get("party_id"))))
            ],
            identifiers=list(document.identifiers or []),
            version_status=version.status,
            version_ordinal=version.ordinal,
            is_latest_final=bool(
                document.latest_final_version_id
                and version.id == document.latest_final_version_id
            ),
            latest_final_version_id=document.latest_final_version_id,
            score=score,
            excerpt=_excerpt(str(source.get("text") or ""), query_terms),
            source_paths=[item.path for item in authorized_sources],
            matched_identifiers=matched_identifiers or [],
            citations=[citation],
        )

    def _warm_identity_map(
        self, sources: Iterable[dict]
    ) -> tuple[dict[str, Document], dict[str, DocumentVersion], dict[str, Project]]:
        """Bulk-load the documents, versions and projects these index rows point at.

        The caller MUST hold the returned maps for as long as it materializes
        hits: the session identity map only weak-references clean instances, so
        without a strong reference the rows bulk-loaded here are collected right
        away and every later ``session.get`` silently turns back into one SQL
        round-trip per row — the exact N+1 this warm-up exists to prevent (and
        exactly how the previous fire-and-forget warm-up quietly failed to).

        An id absent from its map is a stale index row whose SQL row is gone
        (deleted since the last index sweep); callers skip those up front —
        materializing them would return ``None`` anyway, after wasted queries.
        """
        sources = list(sources)
        doc_ids = {source.get("document_id") for source in sources} - {None, ""}
        version_ids = {source.get("document_version_id") for source in sources} - {None, ""}
        project_ids = {source.get("project_id") for source in sources} - {None, ""}
        documents: dict[str, Document] = {}
        versions: dict[str, DocumentVersion] = {}
        projects: dict[str, Project] = {}
        if doc_ids:
            documents = {
                document.id: document
                for document in self.session.scalars(
                    select(Document).where(Document.id.in_(doc_ids))
                )
            }
        # Hit rows surface matter references and party names, so warm those
        # tables for the page too — same identity-map contract as above: the
        # caller holds the maps, per-hit session.get stays a dict lookup.
        matter_ids = {doc.matter_id for doc in documents.values()} - {None, ""}
        party_ids = {
            str(entry.get("party_id"))
            for doc in documents.values()
            for entry in (doc.parties or [])
            if isinstance(entry, dict) and entry.get("party_id")
        }
        self._warm_matters = (
            {
                matter.id: matter
                for matter in self.session.scalars(
                    select(Matter).where(Matter.id.in_(matter_ids))
                )
            }
            if matter_ids
            else {}
        )
        self._warm_parties = (
            {
                party.id: party
                for party in self.session.scalars(
                    select(Party).where(Party.id.in_(party_ids))
                )
            }
            if party_ids
            else {}
        )
        if version_ids:
            versions = {
                version.id: version
                for version in self.session.scalars(
                    select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
                )
            }
        if project_ids:
            projects = {
                project.id: project
                for project in self.session.scalars(
                    select(Project).where(Project.id.in_(project_ids))
                )
            }
        return documents, versions, projects

    def _latest_final_version_id(self, document_id: str) -> str | None:
        document = self.session.get(Document, document_id)
        return document.latest_final_version_id if document else None

    def _rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """LLM rerank the leading fused candidates with ``retrieval.rerank_model``.

        Reordering is confined to the first ``_RERANK_WINDOW`` hits — that is the
        listing the model is shown — but nothing is dropped. This used to return
        only what the model scored, which silently discarded two classes of hit:
        everything past the window (so ``limit=50`` with rerank on could never
        return more than 20 results) and any candidate inside the window the
        model failed to emit a score for. Both are now appended in fused order
        behind the scored ones, so rerank changes the order of a result set and
        never its membership — which is what makes offset paging over it
        coherent.

        On gateway error this raises — no silent fallback to the fused order."""
        if not hits:
            return hits
        candidates = hits[:_RERANK_WINDOW]
        tail = hits[_RERANK_WINDOW:]
        by_version = {hit.version_id: hit for hit in candidates}
        listing = "\n".join(
            f"[{hit.version_id}] {hit.title or ''}: {hit.excerpt}" for hit in candidates
        )
        result = chat_json(
            self.config.retrieval.rerank_model,
            self.config,
            system=(
                "You are a legal relevance rater. Rate each document "
                "by its relevance to the query on a scale from 0 (irrelevant) to 10 "
                "(perfect match). Return exactly one rating for each id."
            ),
            user=f"Query: {query}\n\nDocuments:\n{listing}",
            schema=RerankResult,
            # A reasoning model spends part of its budget on hidden reasoning tokens
            # before emitting anything. On the default budget it produced EMPTY
            # content for every rerank call — 20 scored ids need real room, and a
            # rerank that always fails is a rerank that is never used.
            max_output_tokens=8000,
        )
        scored: list[SearchHit] = []
        rated: set[str] = set()
        for entry in result.scores:
            hit = by_version.get(entry.id)
            if hit is None or entry.id in rated:
                continue
            hit.score = entry.score
            rated.add(entry.id)
            scored.append(hit)
        scored.sort(key=lambda item: item.score, reverse=True)
        unrated = [hit for hit in candidates if hit.version_id not in rated]
        return [*scored, *unrated, *tail]

    def _citation(
        self,
        document: Document,
        version: DocumentVersion,
        sources: list[SourceObject],
        *,
        matched_chunk: dict | None = None,
    ) -> dict:
        """Build the citation contract shared by every evidence-bearing MCP result."""
        return {
            "project": self.project_reference(document.project_id),
            "document": {
                "id": document.id,
                "project_id": document.project_id,
                "matter_id": document.matter_id,
                "title": document.title,
                "doc_type": document.doc_type,
            },
            "version": {
                "id": version.id,
                "ordinal": version.ordinal,
                "status": version.status,
                "content_hash": version.content_hash,
            },
            "source_objects": [
                self._source_reference(source)
                for source in sorted(sources, key=lambda item: (item.path, item.id))
            ],
            "matched_chunk": matched_chunk,
        }

    def _source_reference(self, source_object: SourceObject) -> dict:
        connector = self.session.get(Source, source_object.source_id)
        return {
            "id": source_object.id,
            "source_id": source_object.source_id,
            "external_id": source_object.external_id,
            "path": source_object.path,
            "name": source_object.name,
            "container": source_object.container,
            "source_version_label": source_object.source_version_label,
            "connector": (
                {
                    "id": connector.id,
                    "project_id": connector.project_id,
                    "kind": connector.kind,
                    "display_name": connector.display_name,
                    "provider": connector.provider,
                }
                if connector
                else None
            ),
        }

    def _authorized_sources(self, version_id: str, principals: set[str]) -> list[SourceObject]:
        return self._bulk_authorized_sources([version_id], principals).get(version_id, [])

    def _bulk_authorized_sources(
        self, version_ids: Iterable[str], principals: set[str]
    ) -> dict[str, list[SourceObject]]:
        """Set-based ``_authorized_sources`` for many versions at once.

        Same fail-closed semantics as checking one version at a time — an entry
        exists only for versions the caller may see, and only with the source
        observations they may see — but at a fixed SQL cost regardless of how
        many versions are checked: one ACL-predicate pass, one source fetch, one
        connector bulk-load and (for non-admins) one grant fetch. This is what
        keeps retrieval verification off the per-candidate N+1 path."""
        requested = sorted({version_id for version_id in version_ids if version_id})
        if not requested:
            return {}
        access = AccessService(self.session)
        authorized_ids = set(
            self.session.scalars(
                select(DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.id.in_(requested),
                    access.version_predicate(principals),
                )
            ).all()
        )
        if not authorized_ids:
            return {}
        pairs = self.session.execute(
            select(DocumentVersionSource.version_id, SourceObject)
            .join(SourceObject, SourceObject.id == DocumentVersionSource.source_object_id)
            .where(
                DocumentVersionSource.version_id.in_(sorted(authorized_ids)),
                SourceObject.deleted_at.is_(None),
            )
        ).all()
        sources_by_version: dict[str, list[SourceObject]] = {}
        sources_by_id: dict[str, SourceObject] = {}
        for version_id, source in pairs:
            sources_by_version.setdefault(version_id, []).append(source)
            sources_by_id[source.id] = source
        if not sources_by_id:
            return {}
        # Bulk-load the connectors, then bind each observation's `source`
        # relationship WHILE the loaded rows are strongly referenced: the identity
        # map alone is weak, so an unbound relationship would lazy-load one
        # connector per source object later (in the grants loop below and in
        # citation building) — the N+1 in a different coat.
        connectors = {
            connector.id: connector
            for connector in self.session.scalars(
                select(Source).where(
                    Source.id.in_({source.source_id for source in sources_by_id.values()})
                )
            )
        }
        for source_object in sources_by_id.values():
            connector = connectors.get(source_object.source_id)
            if connector is not None:
                attributes.set_committed_value(source_object, "source", connector)

        if AccessService.is_admin(principals):
            return sources_by_version
        grants_by_source: dict[str, list[SourceObjectGrant]] = {}
        for grant in self.session.scalars(
            select(SourceObjectGrant).where(
                SourceObjectGrant.source_object_id.in_(sorted(sources_by_id))
            )
        ):
            grants_by_source.setdefault(grant.source_object_id, []).append(grant)
        # Must be the EXPANDED set. Mirrored ACLs name source groups ("group:entra:<guid>"),
        # while a caller authenticates as themselves; without the membership expansion this
        # comparison never matches and every mirrored document silently vanishes — the
        # compiler is fail-closed, so the caller sees an empty result, not an error.
        # `version_predicate` above already expands, which is what made this look correct.
        normalized = access.resolve_principals(principals)
        visible_by_version: dict[str, list[SourceObject]] = {}
        for version_id, sources in sources_by_version.items():
            visible: list[SourceObject] = []
            for source in sources:
                grants = grants_by_source.get(source.id, [])
                if not grants:
                    # Only the explicit local-filesystem adapter may delegate an unreadable
                    # ACL to project grants. External connector ACL gaps fail closed.
                    if source.source.kind == "local_fs":
                        visible.append(source)
                    continue
                matching = [item for item in grants if item.principal in normalized]
                if any(item.effect == "deny" for item in matching):
                    continue
                if any(item.effect == "allow" for item in matching):
                    visible.append(source)
            if visible:
                visible_by_version[version_id] = visible
        return visible_by_version

    def _entity_visible(self, entity_type: str, entity_id: str, principals: set[str]) -> bool:
        """Resolve every traversable entity back to at least one authorized source object."""
        if entity_type == "document_version":
            return bool(self._authorized_sources(entity_id, principals))
        if entity_type == "document":
            version_ids = self.session.scalars(
                select(DocumentVersion.id).where(DocumentVersion.document_id == entity_id)
            ).all()
            return bool(self._bulk_authorized_sources(version_ids, principals))
        if entity_type == "thread":
            document_ids = self.session.scalars(
                select(Relation.from_id).where(
                    Relation.kind == "belongs_to_thread",
                    Relation.from_type == "document",
                    Relation.to_type == "thread",
                    Relation.to_id == entity_id,
                )
            ).all()
            return any(self._entity_visible("document", item, principals) for item in document_ids)
        if entity_type == "eval_record":
            record = self.session.get(EvalRecord, entity_id)
            return bool(
                record
                and record.reference_output_ref
                and self._authorized_sources(record.reference_output_ref, principals)
            )
        if entity_type == "decision_record":
            record = self.session.get(DecisionRecord, entity_id)
            if record is None:
                return False
            if record.version_to:
                sources = self._authorized_sources(record.version_to, principals)
                evidence_source_ids = {
                    str(item.get("source_object_id"))
                    for item in (record.source_evidence or [])
                    if isinstance(item, dict) and item.get("source_object_id")
                }
                if evidence_source_ids:
                    sources = [source for source in sources if source.id in evidence_source_ids]
                return bool(sources)
            return bool(
                record.document_id
                and self._entity_visible("document", record.document_id, principals)
            )
        if entity_type == "matter":
            document_ids = self.session.scalars(
                select(Document.id).where(Document.matter_id == entity_id)
            ).all()
            return any(self._entity_visible("document", item, principals) for item in document_ids)
        return False


_KNOWN_DOCUMENT_MIME_TYPES = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".eml": "message/rfc822",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _guess_mime_type(filename: str) -> str | None:
    """Identify common document types even in minimal images without mime.types."""
    suffix = Path(filename).suffix.casefold()
    return _KNOWN_DOCUMENT_MIME_TYPES.get(suffix) or mimetypes.guess_type(filename)[0]


def _dedupe_citations(citations: list[dict]) -> list[dict]:
    """Keep citation payloads stable while preserving distinct source observations."""
    seen: set[tuple] = set()
    result: list[dict] = []
    for citation in citations:
        document = citation.get("document") or {}
        version = citation.get("version") or {}
        source_ids = tuple(
            sorted(
                str(source.get("id"))
                for source in (citation.get("source_objects") or [])
                if source.get("id")
            )
        )
        key = (document.get("id"), version.get("id"), source_ids)
        if key in seen:
            continue
        seen.add(key)
        result.append(citation)
    return result


def _terms(value: str) -> set[str]:
    """Whitespace/punctuation-split tokens for excerpt highlighting — no regex."""
    tokens: set[str] = set()
    for raw in (value or "").split():
        token = raw.strip(".,;:!?()[]{}\"'").casefold()
        if len(token) > 1:
            tokens.add(token)
    return tokens


def _lexical_score(query: set[str], document: set[str]) -> float:
    return len(query & document) / len(query) if query else 0.0


def _excerpt(text: str, terms: set[str], length: int = 320) -> str:
    lowered = text.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, (min(positions) if positions else 0) - 80)
    excerpt = text[start : start + length].strip()
    return ("…" if start else "") + excerpt + ("…" if start + length < len(text) else "")
