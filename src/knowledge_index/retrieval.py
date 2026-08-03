"""Metadata-first retrieval with source ACL enforcement inside every query path."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlalchemy import select
from sqlalchemy.orm import Session, attributes

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Blob,
    DecisionRecord,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    EvalRecord,
    MatterClient,
    MatterParty,
    Matter,
    Project,
    Relation,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.permissions import AccessService, CompiledAccessScope
from knowledge_index.pipeline.providers import chat_json, embed_text, usage_stage
from knowledge_index.retrieval_types import SearchFilters

# Smallest slice of ranked documents authorized per bulk round-trip. Each batch
# costs a fixed handful of set-based statements no matter how many rows it covers,
# so batches are sized generously: index rows that turn out stale or unauthorized
# must not force a second round for a page that one round could have filled.
_VERIFY_BATCH_MIN = 24


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
    source_paths: list[str] = field(default_factory=list)
    matched_identifiers: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "matter_id": self.matter_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "doc_type_label": self.doc_type_label,
            "version_status": self.version_status,
            "score": round(self.score, 6),
            "excerpt": self.excerpt,
            "source_paths": self.source_paths,
            "matched_identifiers": self.matched_identifiers,
            "citations": self.citations,
        }


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

    def search_filter(
        self,
        *,
        principals: set[str],
        filters: SearchFilters | None = None,
        limit: int = 20,
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        return self._search(
            query=None,
            principals=principals,
            filters=filters or SearchFilters(),
            limit=limit,
            scope=scope,
        )

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

        filters = self._resolve_practice_area(filters or SearchFilters())
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
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("semantic query must not be empty")
        return self._search(
            query=query,
            principals=principals,
            filters=filters or SearchFilters(),
            limit=limit,
            scope=scope,
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
    ) -> dict | None:
        """Return graph-ready document context with explicit relation provenance.

        Stored graph edges remain distinguishable from deterministic context edges
        derived from a shared matter or thread.  Every returned document is independently
        authorization-checked and carries an exact citation.
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
        related = related[: max(0, limit)]
        visible_ids = {item["document_id"] for item in related} | {root.id}
        citations_by_document = {
            root.id: root_summary["citations"],
            **{
                item["document_id"]: item["citations"]
                for item in related
            },
        }
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
            edge_citations = _dedupe_citations(
                [
                    *(
                        citations_by_document.get(edge["from"]["id"], [])
                        if edge["from"]["type"] == "document"
                        else []
                    ),
                    *(
                        citations_by_document.get(edge["to"]["id"], [])
                        if edge["to"]["type"] == "document"
                        else []
                    ),
                ]
            )
            visible_explicit_edges.append(
                {**edge, "basis": "stored_relation", "citations": edge_citations}
            )
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
                "citations": _dedupe_citations(
                    [*root_summary["citations"], *item["citations"]]
                ),
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
            "result_count": len(related),
        }

    def traverse(
        self,
        entity_type: str,
        entity_id: str,
        *,
        principals: set[str],
        limit: int = 100,
    ) -> list[dict]:
        if not self._entity_visible(entity_type, entity_id, principals):
            return []
        relations = self.session.scalars(
            select(Relation)
            .where(
                ((Relation.from_type == entity_type) & (Relation.from_id == entity_id))
                | ((Relation.to_type == entity_type) & (Relation.to_id == entity_id))
            )
            .limit(limit)
        ).all()
        visible: list[dict] = []
        for relation in relations:
            if not self._entity_visible(relation.from_type, relation.from_id, principals):
                continue
            if not self._entity_visible(relation.to_type, relation.to_id, principals):
                continue
            visible.append(
                {
                    "kind": relation.kind,
                    "from": {"type": relation.from_type, "id": relation.from_id},
                    "to": {"type": relation.to_type, "id": relation.to_id},
                    "provenance": relation.provenance,
                    "citations": _dedupe_citations(
                        [
                            *self.citations_for_reference(
                                relation.from_type, relation.from_id, principals
                            ),
                            *self.citations_for_reference(
                                relation.to_type, relation.to_id, principals
                            ),
                        ]
                    ),
                }
            )
        return visible

    def list_matters(
        self,
        *,
        principals: set[str],
        limit: int = 100,
        practice_area: str | None = None,
    ) -> list[dict]:
        """Matters visible to the caller; ``practice_area`` filters by ontology
        node with SUBTREE semantics (a parent area matches its children)."""
        try:
            area_scope = self.config.ontology_facet("area_of_law")
            service_scope = self.config.ontology_facet("service")
        except ValueError:
            area_scope = None
            service_scope = None
        matters = self.session.scalars(select(Matter).order_by(Matter.title).limit(limit)).all()
        result: list[dict] = []
        for matter in matters:
            if practice_area is not None and (
                area_scope is None
                or matter.practice_area is None
                or practice_area not in area_scope.ancestors(matter.practice_area)
            ):
                continue
            citations = self.citations_for_matter(matter.id, principals)
            if citations:
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
                        }
                        if matter.matter_kind
                        else None,
                        "visible_versions": len(citations),
                        "citations": citations,
                    }
                )
        return result

    def search_decisions(self, query: str, *, principals: set[str], limit: int = 20) -> list[dict]:
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
        scored.sort(key=lambda item: item[0], reverse=True)
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
                "citations": [citation],
            }
            for score, row, citation in scored[:limit]
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
        scope: CompiledAccessScope | None = None,
    ) -> list[SearchHit]:
        # Query embedding and the optional rerank are both spend on the read path;
        # they belong to "search", not to whichever ingestion stage ran last.
        with usage_stage("search"):
            return self._search_opensearch(
                query=query, principals=principals, filters=filters, limit=limit, scope=scope
            )

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
            "citations": [citation],
        }

    def _resolve_practice_area(self, filters: SearchFilters) -> SearchFilters:
        """Translate a practice_area filter into the matter-id set it covers.

        practice_area lives on Matter, not on the chunk, so it cannot be a term on the
        index. We resolve it here with the same SUBTREE semantics as list_matters (a
        parent area matches its children) and hand the backend a matter-id filter. The
        backend's ACL scope still applies on top, so this set only narrows results — it is
        not an authorization boundary. An empty match is preserved, not dropped: a
        practice_area that covers no matter yields no hits rather than silently widening to
        every matter.
        """
        if not filters.practice_area:
            return filters
        try:
            area_scope = self.config.ontology_facet("area_of_law")
        except ValueError:
            area_scope = None
        matched: list[str] = []
        if area_scope is not None:
            rows = self.session.execute(
                select(Matter.id, Matter.practice_area).where(Matter.practice_area.isnot(None))
            ).all()
            matched = [
                matter_id
                for matter_id, area in rows
                if filters.practice_area in area_scope.ancestors(area)
            ]
        if filters.matter_ids is not None:
            allowed = set(filters.matter_ids)
            matched = [matter_id for matter_id in matched if matter_id in allowed]
        return replace(filters, practice_area=None, matter_ids=matched)

    def _search_opensearch(
        self,
        *,
        query: str | None,
        principals: set[str],
        filters: SearchFilters,
        limit: int,
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

        # Metadata-only search: no query, so no legs to fuse — deterministic order.
        if not query or not query.strip():
            rows = index.search(
                query_vector=None, scope=scope, filters=filters, limit=limit
            )
            return self._materialize_metadata(rows, principals=principals, limit=limit)

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
        pool = max(limit * retrieval.candidate_pool_factor, limit)
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
            # _rerank reorders the fused top-20, so with rerank on the top 20 must
            # be exact, not just the top `limit`.
            needed=max(limit, 20) if retrieval.rerank_enabled else limit,
        )
        hits.sort(key=lambda item: item.score, reverse=True)

        if retrieval.rerank_enabled:
            hits = self._rerank(query, hits)
        return hits[:limit]

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
            version_status=version.status,
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
        """LLM rerank the top-20 fused candidates with ``retrieval.rerank_model``.

        On gateway error this raises — no silent fallback to the fused order."""
        if not hits:
            return hits
        candidates = hits[:20]
        by_version = {hit.version_id: hit for hit in candidates}
        listing = "\n".join(
            f"[{hit.version_id}] {hit.title or ''}: {hit.excerpt}" for hit in candidates
        )
        result = chat_json(
            self.config.retrieval.rerank_model,
            self.config,
            system=(
                "Du bist ein juristischer Relevanz-Bewerter. Bewerte jedes Dokument "
                "nach Relevanz zur Anfrage auf einer Skala von 0 (irrelevant) bis 10 "
                "(perfekt passend). Gib für jede id genau eine Bewertung zurück."
            ),
            user=f"Anfrage: {query}\n\nDokumente:\n{listing}",
            schema=RerankResult,
            # A reasoning model spends part of its budget on hidden reasoning tokens
            # before emitting anything. On the default budget it produced EMPTY
            # content for every rerank call — 20 scored ids need real room, and a
            # rerank that always fails is a rerank that is never used.
            max_output_tokens=8000,
        )
        scored: list[SearchHit] = []
        for entry in result.scores:
            hit = by_version.get(entry.id)
            if hit is None:
                continue
            hit.score = entry.score
            scored.append(hit)
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored

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
