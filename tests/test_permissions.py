from __future__ import annotations

from sqlalchemy.orm import Session

from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Project,
    ProjectGrant,
    Source,
    SourceObject,
)
from knowledge_index.connectors.principals import replace_memberships
from knowledge_index.permissions import AccessService, replace_source_object_grants


def test_empty_project_is_visible_to_its_grantee(session: Session) -> None:
    project = Project(key="P-1", name="Empty project")
    session.add(project)
    session.flush()
    session.add(
        ProjectGrant(
            project_id=project.id,
            principal="user:owner",
            principal_kind="user",
            effect="allow",
            role="owner",
        )
    )
    session.flush()
    assert AccessService(session).visible_project_ids({"user:owner"}) == [project.id]


def test_external_missing_acl_fails_closed_and_empty_scope_stays_empty(
    session: Session,
) -> None:
    allowed = Project(key="P-ALLOWED", name="Allowed")
    forbidden = Project(key="P-FORBIDDEN", name="Forbidden")
    session.add_all([allowed, forbidden])
    session.flush()
    session.add(
        ProjectGrant(
            project_id=allowed.id,
            principal="user:member",
            principal_kind="user",
            effect="allow",
            role="viewer",
        )
    )
    source = Source(
        project_id=allowed.id,
        kind="sharepoint_online",
        display_name="External source",
        provider="native",
    )
    session.add(source)
    session.flush()
    source_object = SourceObject(
        source_id=source.id,
        external_id="doc-1",
        path="doc.txt",
        name="doc.txt",
        acl=None,
    )
    blob = Blob(content_hash="a" * 64, size_bytes=4)
    document = Document(project_id=allowed.id, title="Secret")
    session.add_all([source_object, blob, document])
    session.flush()
    version = DocumentVersion(document_id=document.id, content_hash=blob.content_hash, ordinal=1)
    session.add(version)
    session.flush()
    session.add(DocumentVersionSource(version_id=version.id, source_object_id=source_object.id))
    session.flush()

    access = AccessService(session)
    assert access.visible_document_ids({"user:member"}) == []
    scope = access.compile_scope({"user:member"}, project_ids=[forbidden.id])
    assert scope.project_ids == ()
    assert scope.document_ids == ()
    assert scope.opensearch_filter() == {"match_none": {}}


# ------------------------------------------------------- source principals → identities
#
# A source names people its own way. SharePoint reports "user:anwalt@kanzlei.de" and
# "group:entra:<guid>"; this appliance authenticates callers as "user:<oidc-subject>" and
# "group:<keycloak-group>". Nothing matches until the two are reconciled — and because the
# compiler is fail-closed, a mismatch makes documents invisible rather than over-shared,
# which is the hardest failure to notice.


def _seed_external_document(session: Session, _tmp_path, *, acl: list[dict]) -> str:
    """One document from an externally hosted source, carrying a mirrored ACL.

    No project grant is created: that is the point — it isolates whether a source allow
    alone is enough to make the document readable.
    """
    project = Project(key="P-MATTER", name="Restricted matter")
    session.add(project)
    session.flush()
    source = Source(
        project_id=project.id,
        kind="sharepoint_online",
        display_name="SharePoint",
        provider="native",
    )
    session.add(source)
    session.flush()
    source_object = SourceObject(
        source_id=source.id,
        external_id="doc-shared",
        path="Mandate/Shared.docx",
        name="Shared.docx",
        acl=acl,
    )
    blob = Blob(content_hash="b" * 64, size_bytes=8)
    document = Document(project_id=project.id, title="Shared org-wide")
    session.add_all([source_object, blob, document])
    session.flush()
    version = DocumentVersion(document_id=document.id, content_hash=blob.content_hash, ordinal=1)
    session.add(version)
    session.flush()
    session.add(DocumentVersionSource(version_id=version.id, source_object_id=source_object.id))
    replace_source_object_grants(session, source_object.id, acl)
    session.flush()
    return version.id


def _mirror_membership(session: Session, source_id: str, group: str, member: str) -> None:
    replace_memberships(
        session,
        source_id,
        [{"group_id": group, "member_id": member, "member_type": "user"}],
    )


def test_group_membership_expansion_makes_a_source_group_grant_matchable(
    session: Session,
) -> None:
    source = Source(kind="sharepoint_online", display_name="SP", config={})
    session.add(source)
    session.flush()
    _mirror_membership(session, source.id, "entra:abc", "anwalt@kanzlei.de")

    resolved = AccessService(session).resolve_principals({"user:anwalt@kanzlei.de"})
    # The caller never presented this group; the mirrored directory supplied it.
    assert "group:entra:abc" in resolved


def test_nested_source_groups_are_expanded_transitively(session: Session) -> None:
    source = Source(kind="sharepoint_online", display_name="SP", config={})
    session.add(source)
    session.flush()
    replace_memberships(
        session,
        source.id,
        [
            {"group_id": "entra:inner", "member_id": "anwalt@kanzlei.de", "member_type": "user"},
            {"group_id": "entra:outer", "member_id": "entra:inner", "member_type": "group"},
        ],
    )
    resolved = AccessService(session).resolve_principals({"user:anwalt@kanzlei.de"})
    assert {"group:entra:inner", "group:entra:outer"} <= resolved


def test_memberships_are_replaced_so_a_removed_member_loses_access(session: Session) -> None:
    source = Source(kind="sharepoint_online", display_name="SP", config={})
    session.add(source)
    session.flush()
    _mirror_membership(session, source.id, "entra:abc", "anwalt@kanzlei.de")
    # Re-synced without that person: merging instead of replacing would leave the old
    # edge behind and keep granting access the source has withdrawn.
    _mirror_membership(session, source.id, "entra:abc", "partner@kanzlei.de")

    resolved = AccessService(session).resolve_principals({"user:anwalt@kanzlei.de"})
    assert "group:entra:abc" not in resolved


def test_configured_alias_bridges_a_source_group_to_a_local_group(session: Session) -> None:
    aliases = {"group:litigation": "group:entra:abc"}
    resolved = AccessService(session).resolve_principals({"group:litigation"}, aliases=aliases)
    assert "group:entra:abc" in resolved
    # And in reverse, so a document granted to either principal is reachable.
    reverse = AccessService(session).resolve_principals({"group:entra:abc"}, aliases=aliases)
    assert "group:litigation" in reverse


def test_intersect_mode_stops_an_org_wide_share_defeating_a_matter_wall(
    session: Session, tmp_path
) -> None:
    """An over-broad share at source must not reach past the firm's own restriction.

    Under "sufficient" a mirrored allow is enough on its own, so a document shared with
    the whole organisation becomes readable by every user of this appliance. Under
    "intersect" a local grant is required too.
    """
    version_id = _seed_external_document(
        session,
        tmp_path,
        acl=[
            {
                "principal": "role:authenticated",
                "principal_kind": "role",
                "effect": "allow",
            }
        ],
    )
    principals = {"user:aussenstehender@kanzlei.de", "role:authenticated"}

    permissive = AccessService(session, source_acl_mode="sufficient")
    assert version_id in permissive.visible_version_ids(principals)

    strict = AccessService(session, source_acl_mode="intersect")
    assert version_id not in strict.visible_version_ids(principals)
