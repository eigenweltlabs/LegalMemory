"""Authorization and metadata-scope compilation.

The important invariant is that this module produces the database/OpenSearch filter before
any lexical or vector score is calculated.  Retrieval code must never fetch a global nearest
neighbour set and remove unauthorized hits afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from knowledge_index.db.models import (
    Document,
    DocumentGrant,
    DocumentVersion,
    DocumentVersionSource,
    Project,
    ProjectGrant,
    Source,
    SourceObject,
    SourceObjectGrant,
)

ADMIN_PRINCIPALS = {"role:admin", "system:admin"}
MANAGE_ROLES = {"owner", "admin"}


def canonical_principals(values: Iterable[str]) -> set[str]:
    """Normalize trusted identity claims into stable, index-safe principal strings."""

    return {value.strip().casefold() for value in values if value and value.strip()}


@dataclass(frozen=True)
class CompiledAccessScope:
    """Serializable form used by the OpenSearch adapter and audit records."""

    principals: tuple[str, ...]
    project_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    fingerprint: str

    def opensearch_filter(self) -> dict:
        """Return the strict bool filter that wraps hybrid/vector retrieval."""

        # The SQL authorization query produced these exact document ids.  Keeping that
        # decision as an explicit index filter avoids trying to approximate the local +
        # mirrored ACL intersection inside a vector query.  Empty scopes must stay empty.
        if not self.document_ids:
            return {"match_none": {}}
        filters: list[dict] = [{"terms": {"document_id": list(self.document_ids)}}]
        if self.project_ids:
            filters.append({"terms": {"project_id": list(self.project_ids)}})
        return {"bool": {"filter": filters}}


# Process-wide access defaults, set once from config at startup. Kept here rather than
# threaded through thirteen construction sites so that principal expansion and the ACL
# combination mode cannot be applied inconsistently — a permission check that behaves
# differently depending on which endpoint asked is the worst possible outcome.
_DEFAULTS: dict[str, object] = {"source_acl_mode": "sufficient", "principal_aliases": {}}


def configure_access(*, source_acl_mode: str, principal_aliases: dict[str, str] | None) -> None:
    """Install the security settings every :class:`AccessService` will use."""
    if source_acl_mode not in {"sufficient", "intersect"}:
        raise ValueError(f"unknown source_acl_mode: {source_acl_mode!r}")
    _DEFAULTS["source_acl_mode"] = source_acl_mode
    _DEFAULTS["principal_aliases"] = dict(principal_aliases or {})


class AccessService:
    """Evaluates local project/document grants against mirrored source ACLs.

    ``source_acl_mode`` decides how the two combine for externally hosted sources:

    ``sufficient``  a mirrored source allow is enough on its own. Faithful to the
                    source, but an over-broad share there (a document shared with the
                    whole organisation) makes the document readable by every user of
                    this appliance, bypassing the firm's own matter restrictions.
    ``intersect``   a mirrored source allow AND a local project/document allow are both
                    required. An over-broad share at source can no longer defeat an
                    ethical wall, at the cost of every external source needing local
                    grants before anything is retrievable.

    Local (``local_fs``) sources are unaffected: they have no readable object ACL and
    delegate to the project boundary either way.
    """

    def __init__(
        self,
        session: Session,
        *,
        source_acl_mode: str | None = None,
        principal_aliases: dict[str, str] | None = None,
    ) -> None:
        mode = source_acl_mode or str(_DEFAULTS["source_acl_mode"])
        if mode not in {"sufficient", "intersect"}:
            raise ValueError(f"unknown source_acl_mode: {mode!r}")
        self.session = session
        self.source_acl_mode = mode
        self.principal_aliases = (
            principal_aliases
            if principal_aliases is not None
            else dict(_DEFAULTS["principal_aliases"])  # type: ignore[arg-type]
        )

    def resolve_principals(self, principals: set[str], *, aliases: dict | None = None) -> set[str]:
        """Expand a caller's identity with mirrored group memberships and aliases.

        Source ACLs name people the way the source does. Without this expansion a
        mirrored grant to a source group matches nobody, and because the compiler is
        fail-closed the documents simply vanish instead of erroring.

        Aliases are applied on both sides of the membership lookup, and that is what
        makes them able to fix the mismatch they exist for. A lawyer who signs in as
        ``ursula@firm.de`` but appears in Drive as ``u.schmidt@firm.de`` is bridged by
        an alias — but the bridge is only worth anything if the *source* address is
        what gets looked up in the mirrored memberships, which means aliasing first.
        Aliasing again afterwards keeps the group-to-group case working. Both passes
        are additive and idempotent, so running them twice adds nothing but reach.
        """
        from knowledge_index.connectors.principals import (
            apply_aliases,
            expand_with_memberships,
        )

        effective = aliases if aliases is not None else self.principal_aliases
        normalized = apply_aliases(canonical_principals(principals), effective)
        expanded = expand_with_memberships(self.session, normalized)
        return canonical_principals(apply_aliases(expanded, effective))

    @staticmethod
    def is_admin(principals: set[str]) -> bool:
        normalized = canonical_principals(principals)
        return bool(normalized & ADMIN_PRINCIPALS)

    def version_predicate(self, principals: set[str]) -> ColumnElement[bool]:
        """Correlated SQL predicate for ``Document`` + ``DocumentVersion`` queries.

        Deny wins at every scope.  A caller needs an allow from the project, document, or
        mirrored source object.  If a source object has a known ACL, at least one accessible
        active observation of that version is required, so a local grant cannot punch
        through an external ethical wall. Administrators bypass ACLs, not lifecycle:
        a tombstoned-only version is retained for restoration and audit, but is not a
        live document in the estate.
        """

        normalized = self.resolve_principals(principals)
        active_source_observation = exists(
            select(DocumentVersionSource.version_id)
            .join(
                SourceObject,
                SourceObject.id == DocumentVersionSource.source_object_id,
            )
            .where(
                DocumentVersionSource.version_id == DocumentVersion.id,
                SourceObject.deleted_at.is_(None),
            )
        )
        if self.is_admin(normalized):
            return active_source_observation
        if not normalized:
            return DocumentVersion.id.is_(None)

        values = sorted(normalized)
        project_allow = exists(
            select(ProjectGrant.id).where(
                ProjectGrant.project_id == Document.project_id,
                ProjectGrant.principal.in_(values),
                ProjectGrant.effect == "allow",
            )
        )
        project_deny = exists(
            select(ProjectGrant.id).where(
                ProjectGrant.project_id == Document.project_id,
                ProjectGrant.principal.in_(values),
                ProjectGrant.effect == "deny",
            )
        )
        document_allow = exists(
            select(DocumentGrant.id).where(
                DocumentGrant.document_id == Document.id,
                DocumentGrant.principal.in_(values),
                DocumentGrant.effect == "allow",
            )
        )
        document_deny = exists(
            select(DocumentGrant.id).where(
                DocumentGrant.document_id == Document.id,
                DocumentGrant.principal.in_(values),
                DocumentGrant.effect == "deny",
            )
        )

        source_base = (
            select(SourceObjectGrant.id)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObjectGrant.source_object_id,
            )
            .join(SourceObject, SourceObject.id == SourceObjectGrant.source_object_id)
            .where(
                DocumentVersionSource.version_id == DocumentVersion.id,
                SourceObject.deleted_at.is_(None),
            )
        )
        source_allow = exists(
            source_base.where(
                SourceObjectGrant.principal.in_(values),
                SourceObjectGrant.effect == "allow",
            )
        )
        source_deny = exists(
            source_base.where(
                SourceObjectGrant.principal.in_(values),
                SourceObjectGrant.effect == "deny",
            )
        )

        # A mounted/local source deliberately delegates access to the local project
        # boundary when it has no readable object ACL.  Every external connector is
        # fail-closed until it supplies at least one mirrored allow grant.
        project_scoped_source = exists(
            select(SourceObject.id)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObject.id,
            )
            .join(Source, Source.id == SourceObject.source_id)
            .where(
                DocumentVersionSource.version_id == DocumentVersion.id,
                SourceObject.deleted_at.is_(None),
                Source.kind == "local_fs",
                ~exists(
                    select(SourceObjectGrant.id).where(
                        SourceObjectGrant.source_object_id == SourceObject.id
                    )
                ),
            )
        )

        local_allow = or_(project_allow, document_allow)
        if self.source_acl_mode == "intersect":
            # A local grant is required even when the source says yes, so an over-broad
            # share at source cannot reach past a matter restriction here.
            any_allow = local_allow
        else:
            any_allow = or_(local_allow, source_allow)
        source_intersection = or_(source_allow, project_scoped_source)
        no_deny = and_(~project_deny, ~document_deny, ~source_deny)
        return and_(any_allow, source_intersection, no_deny)

    def visible_version_ids(self, principals: set[str]) -> list[str]:
        return list(
            self.session.scalars(
                select(DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(self.version_predicate(principals))
                .distinct()
            ).all()
        )

    def visible_document_ids(self, principals: set[str]) -> list[str]:
        return list(
            self.session.scalars(
                select(Document.id)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(self.version_predicate(principals))
                .distinct()
            ).all()
        )

    def visible_project_ids(self, principals: set[str]) -> list[str]:
        if self.is_admin(principals):
            return list(self.session.scalars(select(Project.id)).all())
        normalized = canonical_principals(principals)
        if not normalized:
            return []
        values = sorted(normalized)
        allow_grant = aliased(ProjectGrant)
        deny_grant = aliased(ProjectGrant)
        directly_granted = set(
            self.session.scalars(
                select(allow_grant.project_id)
                .where(
                    allow_grant.principal.in_(values),
                    allow_grant.effect == "allow",
                    ~exists(
                        select(deny_grant.id).where(
                            deny_grant.project_id == allow_grant.project_id,
                            deny_grant.principal.in_(values),
                            deny_grant.effect == "deny",
                        )
                    ),
                )
                .distinct()
            ).all()
        )
        document_ids = self.visible_document_ids(principals)
        document_projects = set(
            self.session.scalars(
                select(Document.project_id)
                .where(Document.id.in_(document_ids), Document.project_id.is_not(None))
                .distinct()
            ).all()
        )
        return sorted(directly_granted | document_projects)

    def can_manage_project(self, project_id: str, principals: set[str]) -> bool:
        if self.is_admin(principals):
            return True
        normalized = canonical_principals(principals)
        if not normalized:
            return False
        return (
            self.session.scalar(
                select(ProjectGrant.id).where(
                    ProjectGrant.project_id == project_id,
                    ProjectGrant.principal.in_(sorted(normalized)),
                    ProjectGrant.effect == "allow",
                    ProjectGrant.role.in_(MANAGE_ROLES),
                )
            )
            is not None
        )

    def compile_scope(
        self,
        principals: set[str],
        *,
        project_ids: Iterable[str] = (),
        document_ids: Iterable[str] = (),
    ) -> CompiledAccessScope:
        normalized = tuple(sorted(canonical_principals(principals)))
        visible_projects = set(self.visible_project_ids(set(normalized)))
        visible_documents = set(self.visible_document_ids(set(normalized)))
        requested_projects = {item for item in project_ids if item}
        requested_documents = {item for item in document_ids if item}
        projects = tuple(
            sorted(
                visible_projects & requested_projects if requested_projects else visible_projects
            )
        )
        if requested_projects:
            visible_documents = set(
                self.session.scalars(
                    select(Document.id).where(
                        Document.id.in_(visible_documents), Document.project_id.in_(projects)
                    )
                ).all()
            )
        documents = tuple(
            sorted(
                visible_documents & requested_documents
                if requested_documents
                else visible_documents
            )
        )
        raw = "\n".join((*normalized, "--projects", *projects, "--documents", *documents))
        return CompiledAccessScope(
            principals=normalized,
            project_ids=projects,
            document_ids=documents,
            fingerprint=sha256(raw.encode("utf-8")).hexdigest()[:20],
        )


def replace_source_object_grants(
    session: Session,
    source_object_id: str,
    grants: list[dict] | None,
    *,
    source_id: str | None = None,
) -> bool:
    """Atomically replace the normalized ACL snapshot for one source observation.

    Group principals a connector read from the source are bound to that source (see
    ``connectors.principals.qualify_principal``) — a site's "Owners" group means one
    thing at one firm and something else entirely at the next, and an unbound name
    would let the two match. Grants written by an operator are stored as typed: they
    name principals in the appliance's own namespace, which is already unambiguous.

    Returns whether the effective normalized snapshot changed. Sync uses that signal to
    refresh the access projection without pretending the document bytes changed and
    re-running parsing or model stages.
    """

    normalized: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for item in grants or []:
        principal = str(item.get("principal", "")).strip().casefold()
        effect = str(item.get("effect", item.get("access", "allow"))).casefold()
        if not principal or effect not in {"allow", "deny"} or (principal, effect) in seen:
            continue
        declared_origin = item.get("origin")
        origin = str(declared_origin or "connector")
        if declared_origin == "connector" and source_id:
            from knowledge_index.connectors.principals import qualify_principal

            principal = qualify_principal(principal, source_id)
            # Two source group names can collapse onto one scoped principal only if they
            # were already equal; re-checking keeps the unique constraint satisfied.
            if (principal, effect) in seen:
                continue
        seen.add((principal, effect))
        normalized.append(
            {
                "principal": principal,
                "principal_kind": str(item.get("principal_kind", "group")),
                "effect": effect,
                "origin": origin,
                "external_id": item.get("external_id"),
            }
        )

    existing = session.scalars(
        select(SourceObjectGrant).where(SourceObjectGrant.source_object_id == source_object_id)
    ).all()
    existing_signature = sorted(
        (
            row.principal,
            row.principal_kind,
            row.effect,
            row.origin,
            row.external_id or "",
        )
        for row in existing
    )
    normalized_signature = sorted(
        (
            str(item["principal"]),
            str(item["principal_kind"]),
            str(item["effect"]),
            str(item["origin"]),
            str(item["external_id"] or ""),
        )
        for item in normalized
    )
    if existing_signature == normalized_signature:
        return False

    for row in existing:
        session.delete(row)
    if existing:
        # SQLAlchemy flushes INSERTs before DELETEs within one table; without this
        # flush a changed snapshot re-inserting an unchanged principal can violate
        # uq_source_object_grant before the old row is removed.
        session.flush()
    for item in normalized:
        session.add(
            SourceObjectGrant(
                source_object_id=source_object_id,
                principal=str(item["principal"]),
                principal_kind=str(item["principal_kind"]),
                effect=str(item["effect"]),
                origin=str(item["origin"]),
                external_id=item["external_id"],
            )
        )
    return True
