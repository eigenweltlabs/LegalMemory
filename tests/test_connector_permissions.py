"""End-to-end: an ethical wall enforced over an API-backed source.

The concurrent-load wall test covers ``local_fs`` — which is
the one source kind with a *different* permission predicate, because a mounted folder
delegates to the project boundary while every external source is fail-closed. So the path
that actually matters for a firm's SharePoint estate was the one path never proven.

This exercises the whole chain against recorded Graph responses: connector → mirrored
ACLs → mirrored group memberships → the permission compiler → what a given lawyer can
retrieve. Each test states the wall it is protecting.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.connectors.principals import replace_memberships
from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Project,
    Source,
    SourceObject,
)
from knowledge_index.permissions import AccessService
from knowledge_index.sync import SyncEngine
from tests.connector_replay import Recorded, build
from tests.test_connector_replay import (
    _google_drive_routes,
    _sharepoint_routes,
    _teams_routes,
)

# The people the wall sits between. None of them has a local project grant, so only the
# mirrored ACL can let them in.
#
# GROUP_MEMBER is granted *solely* through the matter's SharePoint group, which is what
# isolates group expansion: with the memberships unmirrored they see nothing.
# NAMED_INSIDER additionally holds a direct grant on the item.
GROUP_MEMBER = {"user:partnerin@kanzlei.de"}
NAMED_INSIDER = {"user:anwalt@kanzlei.de"}
OUTSIDER = {"user:fremder@kanzlei.de"}


def _sync_sharepoint(session: Session, tmp_path) -> Source:
    """Run a real SharePoint scan against recorded responses and apply it to the index."""
    source = Source(kind="sharepoint_online", display_name="Mandate", config={})
    session.add(source)
    session.flush()

    connector, _client = build("sharepoint_online", _sharepoint_routes(), staging=tmp_path)
    try:
        SyncEngine(session, source, connector).sync()
        replace_memberships(session, source.id, connector.memberships())
    finally:
        connector.close()
    session.flush()
    return source


def _promote_to_document(session: Session, source: Source) -> str:
    """Attach the synced source object to a document version, as the pipeline would.

    Retrieval is scoped on document versions, so the mirrored grants only take effect once
    an object is linked to one. This is the minimum wiring that makes the predicate real.
    """
    row = session.scalars(
        select(SourceObject).where(
            SourceObject.source_id == source.id, SourceObject.name.like("%.docx")
        )
    ).first()
    assert row is not None, "the SharePoint scan produced no document-shaped object"

    project = Project(key="P-MANDAT", name="Mandat Schmidt")
    blob = Blob(content_hash="c" * 64, size_bytes=12)
    session.add_all([project, blob])
    session.flush()
    document = Document(project_id=project.id, title=row.name)
    session.add(document)
    session.flush()
    version = DocumentVersion(document_id=document.id, content_hash=blob.content_hash, ordinal=1)
    session.add(version)
    session.flush()
    session.add(DocumentVersionSource(version_id=version.id, source_object_id=row.id))
    session.flush()
    return version.id


def _promote_matching_object(session: Session, source: Source, path_needle: str) -> str:
    row = session.scalars(
        select(SourceObject).where(
            SourceObject.source_id == source.id,
            SourceObject.path.contains(path_needle),
            SourceObject.staged_path.is_not(None),
        )
    ).first()
    assert row is not None, f"the sync produced no document under {path_needle!r}"

    project = Project(key=f"P-{source.kind.upper()}", name=source.display_name)
    blob = Blob(content_hash="d" * 64, size_bytes=12)
    session.add_all([project, blob])
    session.flush()
    document = Document(project_id=project.id, title=row.name)
    session.add(document)
    session.flush()
    version = DocumentVersion(document_id=document.id, content_hash=blob.content_hash, ordinal=1)
    session.add(version)
    session.flush()
    session.add(DocumentVersionSource(version_id=version.id, source_object_id=row.id))
    session.flush()
    return version.id


def test_a_teams_member_reaches_a_standard_channel_without_naming_the_group(
    session: Session, tmp_path
) -> None:
    source = Source(kind="teams", display_name="Teams", config={})
    session.add(source)
    session.flush()
    connector, _client = build("teams", _teams_routes(), staging=tmp_path)
    try:
        # The engine, not test scaffolding, must write the membership snapshot.
        SyncEngine(session, source, connector).sync()
    finally:
        connector.close()
    version_id = _promote_matching_object(session, source, "Allgemein")

    assert version_id in AccessService(session).visible_version_ids(
        {"user:anwalt@kanzlei.de"}
    )
    assert version_id not in AccessService(session).visible_version_ids(
        {"user:fremder@kanzlei.de"}
    )


def _google_group_drive_routes(*, members: list[dict]) -> dict[str, Recorded]:
    routes = _google_drive_routes()
    files = routes["GET https://www.googleapis.com/drive/v3/files"].payload["files"]
    files[0]["permissions"] = [
        {
            "id": "owner",
            "type": "user",
            "role": "owner",
            "emailAddress": "anwalt@kanzlei.de",
        },
        {
            "id": "matter-group",
            "type": "group",
            "role": "reader",
            "emailAddress": "litigation@kanzlei.de",
        },
    ]
    routes[
        "GET https://admin.googleapis.com/admin/directory/v1/groups/"
        "litigation%40kanzlei.de/members"
    ] = Recorded({"members": members})
    return routes


def test_google_group_membership_grants_and_then_revokes_drive_access(
    session: Session, tmp_path
) -> None:
    source = Source(kind="google_drive", display_name="Drive", config={})
    session.add(source)
    session.flush()

    connector, _client = build(
        "google_drive",
        _google_group_drive_routes(
            members=[
                {
                    "email": "partnerin@kanzlei.de",
                    "type": "USER",
                    "status": "ACTIVE",
                }
            ]
        ),
        staging=tmp_path,
    )
    try:
        SyncEngine(session, source, connector).sync()
    finally:
        connector.close()

    version_id = _promote_matching_object(session, source, "Schmidt")
    group_member = {"user:partnerin@kanzlei.de"}
    assert version_id in AccessService(session).visible_version_ids(group_member)
    assert version_id not in AccessService(session).visible_version_ids(OUTSIDER)

    # A later full permission refresh sees the source group without that person. The
    # connector returns an empty membership snapshot and the engine removes the old edge.
    connector, _client = build(
        "google_drive",
        _google_group_drive_routes(members=[]),
        staging=tmp_path,
    )
    try:
        SyncEngine(session, source, connector).sync(force_full=True)
    finally:
        connector.close()
    assert version_id not in AccessService(session).visible_version_ids(group_member)


def test_sync_writes_mirrored_grants_for_an_api_source(session: Session, tmp_path) -> None:
    source = _sync_sharepoint(session, tmp_path)
    objects = session.scalars(
        select(SourceObject).where(SourceObject.source_id == source.id)
    ).all()
    assert objects, "nothing was synced"
    documents = [row for row in objects if row.name.endswith(".docx")]
    assert documents, "no document-shaped object was synced"
    # The grants are what make the document reachable at all; without them it is invisible.
    assert documents[0].acl, "the scan stored no mirrored ACL"


def test_a_lawyer_in_the_matter_group_can_retrieve_the_document(
    session: Session, tmp_path
) -> None:
    source = _sync_sharepoint(session, tmp_path)
    version_id = _promote_to_document(session, source)

    # Granted only via a SharePoint group, so reaching the document depends entirely on
    # the mirrored membership expansion — precisely what used to fail closed and make an
    # entire corpus invisible.
    assert version_id in AccessService(session).visible_version_ids(GROUP_MEMBER)
    # And the directly-granted colleague, who does not need the group at all.
    assert version_id in AccessService(session).visible_version_ids(NAMED_INSIDER)


def test_a_lawyer_outside_the_matter_group_cannot(session: Session, tmp_path) -> None:
    source = _sync_sharepoint(session, tmp_path)
    version_id = _promote_to_document(session, source)

    assert version_id not in AccessService(session).visible_version_ids(OUTSIDER)


def test_removing_someone_from_the_source_group_removes_their_access(
    session: Session, tmp_path
) -> None:
    source = _sync_sharepoint(session, tmp_path)
    version_id = _promote_to_document(session, source)
    assert version_id in AccessService(session).visible_version_ids(GROUP_MEMBER)

    # The group is re-synced without them, as a real membership change would arrive.
    replace_memberships(
        session,
        source.id,
        [
            {
                "group_id": "entra:group-guid-1",
                "member_id": "jemand.anders@kanzlei.de",
                "member_type": "user",
            }
        ],
    )
    session.flush()

    # Revocation at source must revoke here. Mirroring memberships additively would leave
    # the old edge in place and keep serving withdrawn access indefinitely.
    assert version_id not in AccessService(session).visible_version_ids(GROUP_MEMBER)


def test_intersect_mode_requires_a_local_grant_even_for_the_insider(
    session: Session, tmp_path
) -> None:
    """The configurable half of the wall.

    Under ``sufficient`` the source's word is enough. Under ``intersect`` the firm's own
    matter grant is also required, so an over-broad share at the source cannot reach past
    a restriction this appliance holds.
    """
    source = _sync_sharepoint(session, tmp_path)
    version_id = _promote_to_document(session, source)

    permissive = AccessService(session, source_acl_mode="sufficient")
    assert version_id in permissive.visible_version_ids(GROUP_MEMBER)
    strict = AccessService(session, source_acl_mode="intersect")
    assert version_id not in strict.visible_version_ids(GROUP_MEMBER)


@pytest.mark.parametrize("principals", [GROUP_MEMBER, NAMED_INSIDER, OUTSIDER])
def test_an_unknown_acl_is_invisible_to_everyone(session: Session, tmp_path, principals) -> None:
    """A document whose permissions could not be read must reach nobody.

    Fail-closed is the whole basis of the guarantee: an unreadable ACL is treated as
    unknown, never as permissive.
    """
    from knowledge_index.permissions import replace_source_object_grants

    source = _sync_sharepoint(session, tmp_path)
    version_id = _promote_to_document(session, source)
    row = session.scalars(
        select(SourceObject).where(
            SourceObject.source_id == source.id, SourceObject.name.like("%.docx")
        )
    ).first()
    row.acl = None
    replace_source_object_grants(session, row.id, None)
    session.flush()

    assert version_id not in AccessService(session).visible_version_ids(set(principals))


def test_a_user_reaches_mirrored_documents_without_naming_their_group(factory):
    """Authenticating as yourself must be enough — the mirror supplies your groups.

    `_authorized_sources` compared raw caller principals against mirrored grants, which
    name source groups ("group:entra:<guid>"). A real user, who authenticates as
    themselves, matched nothing and every mirrored document silently vanished. The SQL
    `version_predicate` above it *did* expand, so the bug hid behind a check that passed.
    """
    from knowledge_index.permissions import AccessService

    with factory() as session:
        service = AccessService(session)
        # The caller names only themselves; the mirrored membership must supply the group.
        expanded = service.resolve_principals({"user:lit.user@example.com"})
        assert "user:lit.user@example.com" in expanded
