"""Permission-aware, inspectable graph projection for the data explorer."""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from knowledge_index.db.models import (
    Chunk,
    CommunicationThread,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    Project,
    Relation,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.permissions import AccessService, canonical_principals


class GraphService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def projection(
        self,
        *,
        principals: set[str],
        project_id: str | None = None,
        query: str | None = None,
        doc_type: str | None = None,
        matter_id: str | None = None,
        version_status: str | None = None,
        language: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Return every authorized graph entity needed to inspect the corpus.

        The projection includes provenance (source → source object → version), logical
        structure (project → matter → document → version), communication threads, and
        every stored relation whose endpoints are visible.  ``limit`` remains available
        for very large deployments; ``None``/``0`` means the complete projection.
        """

        access = AccessService(self.session)
        accessible_document_ids = access.visible_document_ids(principals)
        statement = select(Document).where(Document.id.in_(accessible_document_ids))
        if project_id:
            statement = statement.where(Document.project_id == project_id)
        if query:
            statement = statement.where(Document.title.ilike(f"%{query.strip()}%"))
        if doc_type:
            statement = statement.where(Document.doc_type == doc_type)
        if matter_id:
            statement = statement.where(Document.matter_id == matter_id)
        if language:
            statement = statement.where(Document.language == language)
        if version_status:
            status_documents = (
                select(DocumentVersion.document_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.status == version_status,
                    access.version_predicate(principals),
                )
                .distinct()
            )
            statement = statement.where(Document.id.in_(status_documents))
        total_documents = int(
            self.session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        ordered = statement.order_by(Document.updated_at.desc(), Document.id)
        if limit and limit > 0:
            ordered = ordered.limit(limit)
        documents = list(self.session.scalars(ordered).all())
        visible_document_ids = {item.id for item in documents}

        versions = list(
            self.session.scalars(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.id.in_(visible_document_ids),
                    access.version_predicate(principals),
                )
                .order_by(
                    DocumentVersion.document_id,
                    DocumentVersion.ordinal.asc().nullslast(),
                    DocumentVersion.created_at,
                )
            ).all()
        )
        visible_version_ids = {item.id for item in versions}
        matter_ids = {item.matter_id for item in documents if item.matter_id}
        project_ids = {item.project_id for item in documents if item.project_id}
        matters = {
            item.id: item
            for item in self.session.scalars(select(Matter).where(Matter.id.in_(matter_ids))).all()
        }
        project_ids.update(item.project_id for item in matters.values() if item.project_id)
        projects = {
            item.id: item
            for item in self.session.scalars(
                select(Project).where(Project.id.in_(project_ids))
            ).all()
        }
        chunk_counts = dict(
            self.session.execute(
                select(DocumentVersion.document_id, func.count(Chunk.id))
                .join(Chunk, Chunk.document_version_id == DocumentVersion.id)
                .where(DocumentVersion.id.in_(visible_version_ids))
                .group_by(DocumentVersion.document_id)
            ).all()
        )
        version_counts = Counter(version.document_id for version in versions)

        version_source_links = list(
            self.session.execute(
                select(
                    DocumentVersionSource.version_id,
                    DocumentVersionSource.source_object_id,
                ).where(DocumentVersionSource.version_id.in_(visible_version_ids))
            ).all()
        )
        candidate_source_ids = {source_id for _, source_id in version_source_links}
        source_objects = {
            item.id: item
            for item in self.session.scalars(
                select(SourceObject).where(
                    SourceObject.id.in_(candidate_source_ids),
                    SourceObject.deleted_at.is_(None),
                )
            ).all()
        }
        connectors = {
            item.id: item
            for item in self.session.scalars(
                select(Source).where(
                    Source.id.in_({item.source_id for item in source_objects.values()})
                )
            ).all()
        }
        grants_by_source: dict[str, list[SourceObjectGrant]] = defaultdict(list)
        if source_objects and not access.is_admin(principals):
            for grant in self.session.scalars(
                select(SourceObjectGrant).where(
                    SourceObjectGrant.source_object_id.in_(source_objects)
                )
            ).all():
                grants_by_source[grant.source_object_id].append(grant)
        normalized_principals = canonical_principals(principals)
        visible_source_ids = {
            source_id
            for source_id, source_object in source_objects.items()
            if _source_visible(
                source_object,
                connectors.get(source_object.source_id),
                grants_by_source.get(source_id, []),
                normalized_principals,
                admin=access.is_admin(principals),
            )
        }
        version_source_links = [
            (version_id, source_id)
            for version_id, source_id in version_source_links
            if source_id in visible_source_ids
        ]
        visible_connector_ids = {
            source_objects[source_id].source_id for source_id in visible_source_ids
        }

        incident_relations = list(
            self.session.scalars(
                select(Relation).where(
                    or_(
                        (Relation.from_type == "document")
                        & Relation.from_id.in_(visible_document_ids),
                        (Relation.to_type == "document")
                        & Relation.to_id.in_(visible_document_ids),
                        (Relation.from_type == "document_version")
                        & Relation.from_id.in_(visible_version_ids),
                        (Relation.to_type == "document_version")
                        & Relation.to_id.in_(visible_version_ids),
                    )
                )
            ).all()
        )
        thread_ids = {
            endpoint_id
            for relation in incident_relations
            for endpoint_type, endpoint_id in (
                (relation.from_type, relation.from_id),
                (relation.to_type, relation.to_id),
            )
            if endpoint_type == "thread"
        }
        threads = {
            item.id: item
            for item in self.session.scalars(
                select(CommunicationThread).where(CommunicationThread.id.in_(thread_ids))
            ).all()
        }
        allowed_endpoints = {
            "document": visible_document_ids,
            "document_version": visible_version_ids,
            "thread": set(threads),
        }
        visible_relations = [
            relation
            for relation in incident_relations
            if relation.from_id in allowed_endpoints.get(relation.from_type, set())
            and relation.to_id in allowed_endpoints.get(relation.to_type, set())
        ]

        nodes: list[dict] = []
        edges: list[dict] = []
        for project in projects.values():
            nodes.append(
                _node(
                    "project",
                    project.id,
                    project.name,
                    30,
                    key=project.key,
                    status=project.status,
                    description=project.description,
                    external_refs=project.external_refs,
                    created_at=project.created_at.isoformat(),
                    updated_at=project.updated_at.isoformat(),
                )
            )
        for matter in matters.values():
            nodes.append(
                _node(
                    "matter",
                    matter.id,
                    matter.title,
                    23,
                    project_id=matter.project_id,
                    reference_numbers=matter.reference_numbers,
                    practice_area=matter.practice_area,
                    matter_kind=matter.matter_kind,
                    status=matter.status,
                    responsible=matter.responsible,
                    time_range=matter.time_range,
                    imported=matter.imported,
                    provenance=matter.provenance,
                )
            )
            if matter.project_id in projects:
                edges.append(_edge("contains", "project", matter.project_id, "matter", matter.id))
        for document in documents:
            nodes.append(
                _node(
                    "document",
                    document.id,
                    document.title or "Untitled document",
                    13,
                    project_id=document.project_id,
                    matter_id=document.matter_id,
                    doc_type=document.doc_type,
                    language=document.language,
                    date=document.doc_date.isoformat() if document.doc_date else None,
                    parties=document.parties,
                    identifiers=document.identifiers,
                    latest_final_version_id=document.latest_final_version_id,
                    provenance=document.provenance,
                    versions=version_counts.get(document.id, 0),
                    chunks=int(chunk_counts.get(document.id, 0)),
                    created_at=document.created_at.isoformat(),
                    updated_at=document.updated_at.isoformat(),
                )
            )
            if document.matter_id in matters:
                edges.append(
                    _edge("contains", "matter", document.matter_id, "document", document.id)
                )
            elif document.project_id in projects:
                edges.append(
                    _edge("contains", "project", document.project_id, "document", document.id)
                )
        for version in versions:
            nodes.append(
                _node(
                    "version",
                    version.id,
                    f"v{version.ordinal or '?'} · {version.status}",
                    8,
                    document_id=version.document_id,
                    ordinal=version.ordinal,
                    status=version.status,
                    content_hash=version.content_hash,
                    status_evidence=version.status_evidence,
                    redline_against=version.redline_against,
                    provenance=version.provenance,
                    created_at=version.created_at.isoformat(),
                    updated_at=version.updated_at.isoformat(),
                )
            )
            edges.append(
                _edge("version_of", "document", version.document_id, "version", version.id)
            )
        for thread in threads.values():
            nodes.append(
                _node(
                    "thread",
                    thread.id,
                    thread.subject_norm or "Communication thread",
                    12,
                    matter_id=thread.matter_id,
                    participants=thread.participants,
                    time_range=thread.time_range,
                    created_at=thread.created_at.isoformat(),
                    updated_at=thread.updated_at.isoformat(),
                )
            )
            if thread.matter_id in matters:
                edges.append(
                    _edge("contains", "matter", thread.matter_id, "thread", thread.id)
                )
        for connector_id in visible_connector_ids:
            connector = connectors[connector_id]
            nodes.append(
                _node(
                    "source",
                    connector.id,
                    connector.display_name,
                    18,
                    project_id=connector.project_id,
                    provider=connector.provider,
                    source_kind=connector.kind,
                    status=connector.status,
                    sync_policy=connector.sync_policy,
                    last_sync_at=(
                        connector.last_sync_at.isoformat() if connector.last_sync_at else None
                    ),
                    last_sync_summary=connector.last_sync_summary,
                )
            )
        for source_id in visible_source_ids:
            source_object = source_objects[source_id]
            nodes.append(
                _node(
                    "source_object",
                    source_object.id,
                    source_object.name,
                    7,
                    source_id=source_object.source_id,
                    external_id=source_object.external_id,
                    path=source_object.path,
                    container=source_object.container,
                    mime_type=source_object.mime_type,
                    size_bytes=source_object.size_bytes,
                    content_hash=source_object.content_hash,
                    source_version_label=source_object.source_version_label,
                    mtime=source_object.mtime.isoformat() if source_object.mtime else None,
                    author_hint=source_object.author_hint,
                    first_seen=source_object.first_seen.isoformat(),
                    last_seen=source_object.last_seen.isoformat(),
                )
            )
            edges.append(
                _edge(
                    "contains",
                    "source",
                    source_object.source_id,
                    "source_object",
                    source_object.id,
                )
            )
        for version_id, source_id in version_source_links:
            edges.append(
                _edge(
                    "observed_as",
                    "version",
                    version_id,
                    "source_object",
                    source_id,
                )
            )
        for relation in visible_relations:
            edges.append(
                _edge(
                    relation.kind,
                    relation.from_type,
                    relation.from_id,
                    relation.to_type,
                    relation.to_id,
                    relation_id=relation.id,
                    provenance=relation.provenance,
                    created_at=relation.created_at.isoformat(),
                    stored=True,
                )
            )

        kind_counts = Counter(node["kind"] for node in nodes)
        edge_counts = Counter(edge["kind"] for edge in edges)
        return {
            "nodes": nodes,
            "edges": edges,
            "summary": {
                "nodes": len(nodes),
                "edges": len(edges),
                "documents": len(documents),
                "total_documents": total_documents,
                "by_kind": dict(sorted(kind_counts.items())),
                "by_edge_kind": dict(sorted(edge_counts.items())),
                "truncated": len(documents) < total_documents,
            },
        }


def _source_visible(
    source_object: SourceObject,
    connector: Source | None,
    grants: list[SourceObjectGrant],
    principals: set[str],
    *,
    admin: bool,
) -> bool:
    if admin:
        return True
    if not grants:
        return bool(connector and connector.kind == "local_fs")
    matching = [grant for grant in grants if grant.principal in principals]
    return not any(grant.effect == "deny" for grant in matching) and any(
        grant.effect == "allow" for grant in matching
    )


def _node(kind: str, entity_id: str, label: str, size: int, **properties) -> dict:
    return {
        "id": f"{kind}:{entity_id}",
        "entity_id": entity_id,
        "kind": kind,
        "label": label,
        "size": size,
        "properties": properties,
    }


def _graph_kind(entity_type: str) -> str:
    return "version" if entity_type == "document_version" else entity_type


def _edge(
    kind: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    **properties,
) -> dict:
    source_kind = _graph_kind(from_type)
    target_kind = _graph_kind(to_type)
    return {
        "id": f"{kind}:{source_kind}:{from_id}:{target_kind}:{to_id}",
        "kind": kind,
        "source": f"{source_kind}:{from_id}",
        "target": f"{target_kind}:{to_id}",
        "properties": properties,
    }
