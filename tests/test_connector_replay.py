"""Every connector driven against recorded API responses.

These run the real connector classes — their request helpers, retry decorators, entity
construction and permission handling — through the same bridge the sync engine uses, with
HTTP replaced by canned payloads. That verifies the parts we could otherwise only claim:
that each connector constructs, authenticates, paginates, produces the entities the index
expects, and survives a per-item API failure.

What these do not prove is that the recorded payloads still match what Microsoft, Google
or Atlassian send today. Only a live tenant sync shows that.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from knowledge_index.connectors.registry import CATALOG
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.types import NodeSelectionData
from tests.connector_replay import Recorded, ReplayClient, UnexpectedRequest, build

GRAPH = "https://graph.microsoft.com/v1.0"


# --------------------------------------------------------------------- validate() sweep
#
# One call per connector: cheap to record, and it exercises registry construction, config
# parsing, auth injection and each connector's own status handling.

CLIO = "https://eu.app.clio.com/api/v4"

VALIDATE_ROUTES: dict[str, dict[str, Recorded]] = {
    "onedrive": {f"GET {GRAPH}/me/drive": Recorded({"id": "drive-1", "driveType": "business"})},
    "clio": {
        f"GET {CLIO}/users/who_am_i.json": Recorded({"data": {"id": 1, "email": "a@k.de"}})
    },
    "teams": {f"GET {GRAPH}/me/joinedTeams": Recorded({"value": []})},
    "outlook_mail": {f"GET {GRAPH}/me/mailFolders": Recorded({"value": []})},
    "outlook_calendar": {f"GET {GRAPH}/me/calendars": Recorded({"value": []})},
    "onenote": {f"GET {GRAPH}/me/onenote/notebooks": Recorded({"value": []})},
    "sharepoint_online": {f"GET {GRAPH}/sites/root": Recorded({"id": "site-root"})},
    "google_drive": {
        "GET https://www.googleapis.com/drive/v3/drives": Recorded({"drives": []}),
        "GET https://admin.googleapis.com/admin/directory/v1/groups": Recorded({"groups": []}),
    },
    "google_docs": {"GET https://www.googleapis.com/drive/v3/about": Recorded({"user": {}})},
    "gmail": {
        "GET https://gmail.googleapis.com/gmail/v1/users/me/profile": Recorded(
            {"emailAddress": "anwalt@kanzlei.de"}
        )
    },
    "dropbox": {
        "POST https://api.dropboxapi.com/2/users/get_current_account": Recorded(
            {"account_id": "dbid:1"}
        )
    },
    "box": {"GET https://api.box.com/2.0/users/me": Recorded({"id": "1"})},
    "notion": {"GET https://api.notion.com/v1/users/me": Recorded({"id": "user-1"})},
    "slack": {"GET https://slack.com/api/auth.test": Recorded({"ok": True, "team": "Kanzlei"})},
    "confluence": {
        "GET https://api.atlassian.com/oauth/token/accessible-resources": Recorded(
            [{"id": "cloud-1", "url": "https://kanzlei.atlassian.net"}]
        )
    },
}


@pytest.mark.parametrize("short_name", sorted(VALIDATE_ROUTES))
def test_connector_validates_against_recorded_response(short_name, tmp_path):
    connector, client = build(short_name, VALIDATE_ROUTES[short_name], staging=tmp_path)
    try:
        connector._runner.run(connector._source.validate())
    finally:
        connector.close()
    assert client.requests, f"{short_name} made no request during validate()"


def test_every_catalogued_connector_has_a_validate_fixture():
    # A connector added without one would ship entirely unexercised.
    missing = sorted({spec.short_name for spec in CATALOG} - set(VALIDATE_ROUTES))
    assert missing == [], f"connectors with no replay coverage: {missing}"


@pytest.mark.parametrize(
    ("short_name", "route"),
    [("onedrive", f"GET {GRAPH}/me/drive"), ("notion", "GET https://api.notion.com/v1/users/me")],
)
def test_dead_credentials_surface_as_an_auth_error_not_an_empty_sync(short_name, route, tmp_path):
    # Critical: a 401 must never look like "the source is empty", or the engine's tombstone
    # path would delete the firm's index on an expired token.
    connector, _client = build(
        short_name, {route: Recorded({"error": "unauthenticated"}, status=401)}, staging=tmp_path
    )
    try:
        with pytest.raises(SourceAuthError):
            connector._runner.run(connector._source.validate())
    finally:
        connector.close()


# ------------------------------------------------------------------- SharePoint Online
#
# The connector that carries the product's defining behaviour: mirrored ACLs, expanded
# groups, and a delta feed.

SITE = {
    "id": "kanzlei.sharepoint.com,site-guid,web-guid",
    "displayName": "Mandate",
    "webUrl": "https://kanzlei.sharepoint.com/sites/Mandate",
    "name": "Mandate",
}
DRIVE = {
    "id": "drive-1",
    "name": "Dokumente",
    "driveType": "documentLibrary",
    "webUrl": "https://kanzlei.sharepoint.com/sites/Mandate/Dokumente",
    "owner": {"group": {"id": "group-guid-1"}},
}
FILE_ITEM = {
    "id": "item-1",
    "name": "Kaufvertrag.docx",
    "size": 12,
    "eTag": '"{ETAG},1"',
    "cTag": '"c:{CTAG},1"',
    "webUrl": "https://kanzlei.sharepoint.com/sites/Mandate/Kaufvertrag.docx",
    "createdDateTime": "2026-01-05T09:00:00Z",
    "lastModifiedDateTime": "2026-03-01T12:00:00Z",
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "parentReference": {"driveId": "drive-1", "path": "/drive/root:"},
}
SITE_PERMISSIONS = {
    "value": [
        {
            "roles": ["read"],
            "grantedToV2": {"user": {"email": "Anwalt@Kanzlei.de", "id": "u-1"}},
        },
        {
            "roles": ["read"],
            "grantedToV2": {"group": {"id": "group-guid-1"}},
        },
    ]
}


def _sharepoint_routes() -> dict[str, Recorded]:
    """Enough of Graph for one site, one library, one file, with permissions."""
    return {
        f"GET {GRAPH}/sites/root": Recorded(SITE),
        f"GET {GRAPH}/sites/getAllSites": Recorded({"value": [SITE]}),
        f"GET {GRAPH}/sites": Recorded({"value": [SITE]}),
        f"GET {GRAPH}/sites/{SITE['id']}/sites": Recorded({"value": []}),
        f"GET {GRAPH}/sites/{SITE['id']}/drives": Recorded({"value": [DRIVE]}),
        f"GET {GRAPH}/sites/{SITE['id']}/permissions": Recorded(SITE_PERMISSIONS),
        f"GET {GRAPH}/sites/{SITE['id']}/pages": Recorded({"value": []}),
        f"GET {GRAPH}/sites/{SITE['id']}/lists": Recorded({"value": []}),
        f"GET {GRAPH}/drives/drive-1/root/children": Recorded({"value": [FILE_ITEM]}),
        f"GET {GRAPH}/drives/drive-1/items/root/children": Recorded({"value": [FILE_ITEM]}),
        f"GET {GRAPH}/drives/drive-1/root/delta": Recorded(
            {"value": [FILE_ITEM], "@odata.deltaLink": f"{GRAPH}/drives/drive-1/root/delta?token=T2"}
        ),
        f"GET {GRAPH}/drives/drive-1/items/item-1/permissions": Recorded(SITE_PERMISSIONS),
        f"GET {GRAPH}/drives/drive-1/items/item-1/content": Recorded(
            content=b"PK\x03\x04 docx bytes"
        ),
        f"GET {GRAPH}/drives/drive-1": Recorded(DRIVE),
        f"GET {GRAPH}/groups/group-guid-1/members": Recorded(
            {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.user",
                        "id": "u-1",
                        "mail": "Anwalt@Kanzlei.de",
                        "userPrincipalName": "anwalt@kanzlei.de",
                    },
                    {
                        # Group membership only, no direct grant on the item — the case
                        # that isolates whether group expansion is doing the work.
                        "@odata.type": "#microsoft.graph.user",
                        "id": "u-2",
                        "mail": "Partnerin@Kanzlei.de",
                        "userPrincipalName": "partnerin@kanzlei.de",
                    },
                ]
            }
        ),
        f"GET {GRAPH}/groups/group-guid-1/transitiveMembers": Recorded(
            {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.user",
                        "id": "u-1",
                        "mail": "Anwalt@Kanzlei.de",
                    }
                ]
            }
        ),
        f"GET {GRAPH}/groups/group-guid-1": Recorded(
            {"id": "group-guid-1", "displayName": "Mandate Team"}
        ),
        f"GET {GRAPH}/directoryObjects/getByIds": Recorded({"value": []}),
        f"POST {GRAPH}/directoryObjects/getByIds": Recorded({"value": []}),
        f"GET {GRAPH}/users/u-1": Recorded({"id": "u-1", "mail": "Anwalt@Kanzlei.de"}),
        f"GET {GRAPH}/users": Recorded(
            {"value": [{"id": "u-1", "mail": "Anwalt@Kanzlei.de"}]}
        ),
    }


def test_sharepoint_mirrors_item_permissions_onto_observations(tmp_path):
    connector, _client = build("sharepoint_online", _sharepoint_routes(), staging=tmp_path)
    try:
        observations = list(connector.full_scan())
    finally:
        connector.close()

    documents = [item for item in observations if item.name.endswith(".docx")]
    assert documents, "no document observation was produced"
    grants = documents[0].acl
    # The whole point of this connector: a real mirrored ACL, not None.
    assert grants, "SharePoint produced no mirrored ACL"
    principals = {grant["principal"] for grant in grants}
    assert "user:anwalt@kanzlei.de" in principals  # casefolded by the translator
    assert any(principal.startswith("group:") for principal in principals)
    assert all(grant["origin"] == "connector" for grant in grants)


def test_sharepoint_stages_content_so_fetch_needs_no_further_api_calls(tmp_path):
    connector, client = build("sharepoint_online", _sharepoint_routes(), staging=tmp_path)
    try:
        documents = [item for item in connector.full_scan() if item.name.endswith(".docx")]
        assert documents[0].staged_path, "content was not staged during the scan"
        before = len(client.requests)
        with connector.open_staged(documents[0].staged_path) as handle:
            assert handle.read() == b"PK\x03\x04 docx bytes"
        # Reading content must not touch the API again — that was the quadratic bug.
        assert len(client.requests) == before
    finally:
        connector.close()


def test_sharepoint_reports_group_memberships_for_permission_expansion(tmp_path):
    connector, _client = build("sharepoint_online", _sharepoint_routes(), staging=tmp_path)
    try:
        list(connector.full_scan())  # membership collection is populated during traversal
        memberships = connector.memberships()
    finally:
        connector.close()
    # Without these, a group grant matches nobody and the documents stay invisible.
    assert memberships, "no memberships were mirrored"
    assert all({"member_id", "member_type", "group_id"} <= set(row) for row in memberships)
    assert all(row["member_id"] == row["member_id"].casefold() for row in memberships)


def test_sharepoint_change_hint_is_the_source_version_token(tmp_path):
    connector, _client = build("sharepoint_online", _sharepoint_routes(), staging=tmp_path)
    try:
        documents = [item for item in connector.full_scan() if item.name.endswith(".docx")]
    finally:
        connector.close()
    assert documents[0].change_hint, "no change hint: every rescan would re-download"


# ------------------------------------------------------------------------- assertions
#
# Every ACL-mirroring connector is checked the same two ways, because the two failures are
# opposite and both silent: principals that are absent hide the corpus, and an empty ACL
# where the lookup merely failed claims a restriction nobody made.


def _matching(observations, needle: str) -> list:
    """The observations whose path places them under ``needle``."""
    matches = [item for item in observations if needle in item.path]
    assert matches, f"no observation was produced under {needle!r}"
    return matches


def _principals(observations, needle: str) -> set[str]:
    """The mirrored principals on every observation under ``needle``."""
    matches = _matching(observations, needle)
    for item in matches:
        assert item.acl, f"{item.path} carries no mirrored ACL, so nobody can retrieve it"
        assert all(grant["origin"] == "connector" for grant in item.acl), item.path
    return {grant["principal"] for item in matches for grant in item.acl}


def _assert_unknown_but_present(observations, needle: str) -> None:
    """A failed permission read must leave access unknown, never drop the document.

    Unknown (None) and "nobody" ([]) are different claims, and absence is worse than
    either: a full scan tombstones what it does not see, so a transient failure would
    delete real documents.
    """
    for item in _matching(observations, needle):
        assert item.acl is None, f"{item.path} asserted an ACL after a failed lookup"


def _scan(short_name: str, routes: dict, tmp_path, config: dict | None = None) -> list:
    connector, _client = build(short_name, routes, staging=tmp_path, config=config)
    try:
        return list(connector.full_scan())
    finally:
        connector.close()


# ------------------------------------------------------------------------------- Teams
#
# A Teams message has no permission list of its own. What it has is the audience of the
# conversation it was posted in: a standard channel is the whole team, a private channel
# and a chat are their own members. Mirroring a private channel as the team would publish
# a matter-specific conversation to the firm.

TEAM = {"id": "team-1", "displayName": "Mandate"}
STANDARD_CHANNEL = {"id": "channel-1", "displayName": "Allgemein", "membershipType": "standard"}
PRIVATE_CHANNEL = {
    "id": "channel-2",
    "displayName": "Mandat-Schmidt",
    "membershipType": "private",
}
CHAT = {"id": "chat-1", "chatType": "oneOnOne"}


def _teams_message(identifier: str, text: str) -> dict:
    return {
        "id": identifier,
        "createdDateTime": "2026-03-01T12:00:00Z",
        "lastModifiedDateTime": "2026-03-01T12:05:00Z",
        "body": {"content": text, "contentType": "text"},
        "from": {"user": {"displayName": "Anwalt", "id": "u-1"}},
    }


def _teams_routes() -> dict[str, Recorded]:
    """One team with a standard and a private channel, plus a one-to-one chat."""
    return {
        f"GET {GRAPH}/me/joinedTeams": Recorded({"value": [TEAM]}),
        # A team IS its backing Entra group, and this is where that id is read back.
        f"GET {GRAPH}/teams/team-1": Recorded(TEAM),
        f"GET {GRAPH}/teams/team-1/channels": Recorded(
            {"value": [STANDARD_CHANNEL, PRIVATE_CHANNEL]}
        ),
        f"GET {GRAPH}/teams/team-1/channels/channel-1/messages": Recorded(
            {"value": [_teams_message("msg-1", "Die Frist läuft am Freitag ab.")]}
        ),
        f"GET {GRAPH}/teams/team-1/channels/channel-2/messages": Recorded(
            {"value": [_teams_message("msg-2", "Vergleichsangebot im Mandat Schmidt.")]}
        ),
        f"GET {GRAPH}/teams/team-1/channels/channel-2/members": Recorded(
            {"value": [{"userId": "u-2", "email": "Partnerin@Kanzlei.de"}]}
        ),
        f"GET {GRAPH}/teams/team-1/members": Recorded(
            {
                "value": [
                    {
                        "id": "u-1",
                        "email": "Anwalt@Kanzlei.de",
                    }
                ]
            }
        ),
        f"GET {GRAPH}/me/chats": Recorded({"value": [CHAT]}),
        f"GET {GRAPH}/chats/chat-1/messages": Recorded(
            {"value": [_teams_message("msg-3", "Kurz telefoniert wegen der Akte.")]}
        ),
        f"GET {GRAPH}/chats/chat-1/members": Recorded(
            {"value": [{"userId": "u-1", "email": "anwalt@kanzlei.de"}]}
        ),
        f"GET {GRAPH}/users": Recorded(
            {"value": [{"id": "u-1", "mail": "anwalt@kanzlei.de", "displayName": "Anwalt"}]}
        ),
    }


def test_teams_produces_text_entities_that_get_staged(tmp_path):
    observations = _scan("teams", _teams_routes(), tmp_path)

    assert observations, "Teams produced nothing indexable"
    staged = [item for item in observations if item.staged_path]
    assert staged, "message text was not staged for the pipeline"
    bodies = ""
    for item in staged:
        with open(item.staged_path, encoding="utf-8") as handle:
            bodies += handle.read()
    assert "Frist" in bodies


def test_teams_mirrors_the_channel_audience_onto_its_messages(tmp_path):
    observations = _scan("teams", _teams_routes(), tmp_path)

    # A standard channel is the whole team, emitted as the group so mirrored memberships
    # expand it. Enumerating the team here would go stale on the next hire.
    assert "group:entra:team-1" in _principals(observations, "Allgemein")

    # A private channel is its own members and must not inherit the team.
    private = _principals(observations, "Mandat-Schmidt")
    assert "user:partnerin@kanzlei.de" in private  # casefolded by the translator
    assert "group:entra:team-1" not in private, "a private channel was published to the team"
    assert "role:authenticated" not in private, "a private channel was published firm-wide"


def test_teams_mirrors_chat_participants_onto_chat_messages(tmp_path):
    # A chat is the most private thing Teams holds; it may reach its participants only.
    observations = _scan("teams", _teams_routes(), tmp_path)
    assert _principals(observations, "oneOnOne chat") == {"user:anwalt@kanzlei.de"}


def test_teams_expands_standard_channel_groups_to_their_members(tmp_path):
    connector, client = build("teams", _teams_routes(), staging=tmp_path)
    try:
        list(connector.full_scan())
        memberships = connector.memberships()
    finally:
        connector.close()

    assert memberships == [
        {
            "member_id": "anwalt@kanzlei.de",
            "member_type": "user",
            "group_id": "entra:team-1",
            "group_name": "Mandate",
        }
    ]
    assert client.called("/teams/team-1/members")


@pytest.mark.parametrize(
    ("route", "needle"),
    [
        # The team read that resolves the group behind a standard channel.
        (f"GET {GRAPH}/teams/team-1", "Allgemein"),
        (f"GET {GRAPH}/teams/team-1/channels/channel-2/members", "Mandat-Schmidt"),
        (f"GET {GRAPH}/chats/chat-1/members", "oneOnOne chat"),
    ],
)
def test_a_failed_teams_membership_read_leaves_access_unknown(route, needle, tmp_path):
    routes = _teams_routes()
    routes[route] = Recorded({"error": {"code": "accessDenied"}}, status=403)
    observations = _scan("teams", routes, tmp_path)
    _assert_unknown_but_present(observations, needle)
    # And the rest of the scan is unaffected: one unreadable membership is not a sync
    # failure, it is one conversation left fail-closed.
    assert len(observations) > len(_matching(observations, needle))


def test_teams_permission_mirroring_can_be_turned_off(tmp_path):
    # With the flag off no membership call is made at all, and everything stays unknown
    # rather than quietly becoming readable.
    connector, client = build(
        "teams", _teams_routes(), staging=tmp_path, config={"mirror_permissions": False}
    )
    try:
        observations = list(connector.full_scan())
    finally:
        connector.close()
    assert observations, "Teams produced nothing indexable"
    assert all(item.acl is None for item in observations)
    assert not client.called("/members")


# -------------------------------------------------------------------------- Confluence
#
# Confluence inverts the usual model: content with no read restriction is visible to
# everyone who can see the space. Reading an empty restriction set as "nobody" would black
# out an entire wiki, so that case is pinned here as firm-wide rather than as empty.

CONFLUENCE_BASE = "https://api.atlassian.com/ex/confluence/cloud-1"
SPACE = {"id": "space-1", "key": "MANDATE", "name": "Mandate", "type": "global"}


def _confluence_page_detail(page_id: str, title: str) -> dict:
    return {
        "id": page_id,
        "title": title,
        "status": "current",
        "createdAt": "2026-01-05T09:00:00Z",
        "version": {"number": 3, "createdAt": "2026-03-01T12:00:00Z"},
        "body": {"storage": {"value": f"<p>{title}: Fristen und Vollmacht.</p>"}},
    }


def _confluence_restrictions(*, users=(), groups=()) -> dict:
    """A ``restriction/byOperation`` payload, keyed by operation as Confluence returns it."""
    return {
        "read": {
            "operation": "read",
            "restrictions": {
                "user": {"results": [{"email": email, "accountId": "acc"} for email in users]},
                "group": {"results": [{"name": name, "id": "gid"} for name in groups]},
            },
        },
        "update": {"operation": "update", "restrictions": {}},
    }


def _confluence_routes() -> dict[str, Recorded]:
    """One space with a restricted page and an unrestricted one, plus a blog post."""
    return {
        "GET https://api.atlassian.com/oauth/token/accessible-resources": Recorded(
            [{"id": "cloud-1", "url": "https://kanzlei.atlassian.net"}]
        ),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/spaces": Recorded({"results": [SPACE]}),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/spaces/space-1/pages": Recorded(
            {"results": [{"id": "page-1"}, {"id": "page-2"}]}
        ),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/pages/page-1?": Recorded(
            _confluence_page_detail("page-1", "Mandat-Schmidt")
        ),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/pages/page-2?": Recorded(
            _confluence_page_detail("page-2", "Kanzlei-Handbuch")
        ),
        # Restricted to one lawyer and one group.
        f"GET {CONFLUENCE_BASE}/wiki/rest/api/content/page-1/restriction": Recorded(
            _confluence_restrictions(users=["Anwalt@Kanzlei.de"], groups=["mandate-team"])
        ),
        # No restriction at all: space-wide, which is a grant and not a blackout.
        f"GET {CONFLUENCE_BASE}/wiki/rest/api/content/page-2/restriction": Recorded(
            _confluence_restrictions()
        ),
        f"GET {CONFLUENCE_BASE}/wiki/rest/api/content/blog-1/restriction": Recorded(
            _confluence_restrictions(users=["Anwalt@Kanzlei.de"])
        ),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/pages/page-1/inline-comments": Recorded(
            {
                "results": [
                    {
                        "id": "comment-1",
                        "status": "current",
                        "container": {"id": "page-1"},
                        "body": {"storage": {"value": "<p>Frist notiert.</p>"}},
                        "createdAt": "2026-03-02T08:00:00Z",
                    }
                ]
            }
        ),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/pages/page-2/inline-comments": Recorded(
            {"results": []}
        ),
        f"GET {CONFLUENCE_BASE}/wiki/api/v2/spaces/space-1/blogposts": Recorded(
            {
                "results": [
                    {
                        "id": "blog-1",
                        "title": "Neue Rechtsprechung",
                        "status": "current",
                        "createdAt": "2026-02-01T09:00:00Z",
                    }
                ]
            }
        ),
    }


def test_confluence_mirrors_page_read_restrictions(tmp_path):
    observations = _scan("confluence", _confluence_routes(), tmp_path)

    restricted = _principals(observations, "Mandat-Schmidt")
    assert "user:anwalt@kanzlei.de" in restricted  # casefolded by the translator
    assert "group:confluence:mandate-team" in restricted
    # A restricted page must not also be space-wide, or the restriction means nothing.
    assert "role:authenticated" not in restricted


def test_confluence_treats_an_unrestricted_page_as_space_wide_not_invisible(tmp_path):
    # The failure this guards against blacks out a whole wiki: Confluence expresses
    # "everyone in the space" as the absence of a restriction, not as a viewer list.
    assert "role:authenticated" in _principals(
        _scan("confluence", _confluence_routes(), tmp_path), "Kanzlei-Handbuch"
    )


def test_confluence_comments_inherit_the_page_they_sit_on(tmp_path):
    # A comment on a restricted page is as confidential as the page. Indexing it with the
    # space's audience would leak the restricted discussion.
    observations = _scan("confluence", _confluence_routes(), tmp_path)
    # Comments hang below the page in the breadcrumb path, which is how they are told apart
    # from the page itself.
    comments = [item for item in observations if "Mandat-Schmidt/" in item.path]
    assert comments, "no comment observation was produced"
    for item in comments:
        assert item.acl, f"{item.path} carries no mirrored ACL"
        principals = {grant["principal"] for grant in item.acl}
        assert "user:anwalt@kanzlei.de" in principals
        assert "role:authenticated" not in principals


def test_a_failed_confluence_restriction_read_leaves_the_page_unknown(tmp_path):
    routes = _confluence_routes()
    routes[f"GET {CONFLUENCE_BASE}/wiki/rest/api/content/page-1/restriction"] = Recorded(
        {"message": "Not permitted"}, status=403
    )
    observations = _scan("confluence", routes, tmp_path)
    _assert_unknown_but_present(observations, "Mandat-Schmidt")
    # The unrestricted page in the same space is unaffected — one 403 is not a sync failure.
    assert _principals(observations, "Kanzlei-Handbuch")


def test_confluence_permission_mirroring_can_be_turned_off(tmp_path):
    connector, client = build(
        "confluence", _confluence_routes(), staging=tmp_path, config={"mirror_permissions": False}
    )
    try:
        observations = list(connector.full_scan())
    finally:
        connector.close()
    assert observations, "Confluence produced nothing indexable"
    assert all(item.acl is None for item in observations)
    assert not client.called("/restriction")


# ------------------------------------------------------------------------- Google Docs


def _google_docs_file(identifier: str, name: str, permissions) -> dict:
    file_data = {
        "id": identifier,
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
        "createdTime": "2026-01-05T09:00:00.000Z",
        "modifiedTime": "2026-03-01T12:00:00.000Z",
        "webViewLink": f"https://docs.google.com/document/d/{identifier}/edit",
        "size": 12,
    }
    if permissions is not None:
        file_data["permissions"] = permissions
    return file_data


GOOGLE_DOCS_PERMISSIONS = [
    {"id": "p1", "type": "user", "role": "writer", "emailAddress": "Anwalt@Kanzlei.de"},
    {"id": "p2", "type": "group", "role": "reader", "emailAddress": "mandate@kanzlei.de"},
    # Neither of these confers read to a person here: an "anyone" link is an exposure to
    # report rather than a grant, and a pending owner has not accepted anything.
    {"id": "p3", "type": "anyone", "role": "reader"},
    {"id": "p4", "type": "user", "role": "writer", "emailAddress": "x@y.de", "pendingOwner": True},
]


def _google_docs_routes(*, permissions=GOOGLE_DOCS_PERMISSIONS) -> dict[str, Recorded]:
    return {
        "GET https://www.googleapis.com/drive/v3/about": Recorded({"user": {}}),
        "GET https://www.googleapis.com/drive/v3/changes/startPageToken": Recorded(
            {"startPageToken": "T1"}
        ),
        "GET https://www.googleapis.com/drive/v3/files": Recorded(
            {"files": [_google_docs_file("doc-1", "Kaufvertrag", permissions)]}
        ),
        "GET https://www.googleapis.com/drive/v3/files/doc-1/export": Recorded(
            content=b"PK\x03\x04 docx bytes"
        ),
    }


def test_google_docs_mirrors_drive_sharing_permissions(tmp_path):
    principals = _principals(_scan("google_docs", _google_docs_routes(), tmp_path), "Kaufvertrag")
    assert "user:anwalt@kanzlei.de" in principals
    assert "group:google:mandate@kanzlei.de" in principals
    # A public link is not a grant to mirror, and a pending owner is not access.
    assert "role:authenticated" not in principals
    assert "user:x@y.de" not in principals


def test_google_docs_domain_sharing_becomes_firm_wide(tmp_path):
    routes = _google_docs_routes(
        permissions=[{"id": "p1", "type": "domain", "role": "reader", "domain": "kanzlei.de"}]
    )
    assert "role:authenticated" in _principals(
        _scan("google_docs", routes, tmp_path), "Kaufvertrag"
    )


def test_google_docs_without_permissions_stays_unknown_not_empty(tmp_path):
    # Drive omits the permissions sub-resource when the signed-in account may not see it.
    # That is a capability gap, not a document nobody may read.
    observations = _scan("google_docs", _google_docs_routes(permissions=None), tmp_path)
    _assert_unknown_but_present(observations, "Kaufvertrag")


def test_google_docs_permission_mirroring_can_be_turned_off(tmp_path):
    observations = _scan(
        "google_docs", _google_docs_routes(), tmp_path, config={"mirror_permissions": False}
    )
    assert observations, "Google Docs produced nothing indexable"
    assert all(item.acl is None for item in observations)


# ------------------------------------------------------------- owner-scoped corpora
#
# A mailbox, a calendar and a notebook belong to one person. That is a real ACL, not the
# absence of one, and leaving it unset made every one of these connectors produce documents
# nobody could retrieve. The owner is resolved once per run: a lookup per item would spend
# quota re-learning the same address, so the call count is asserted too.

OWNER = "anwalt@kanzlei.de"
GRAPH_ME = {"id": "u-1", "mail": "Anwalt@Kanzlei.de", "userPrincipalName": "anwalt@kanzlei.de"}


def _gmail_routes() -> dict[str, Recorded]:
    message = {
        "id": "m-1",
        "threadId": "t-1",
        "internalDate": "1772366400000",
        "labelIds": ["INBOX"],
        "sizeEstimate": 42,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Fristverlängerung"},
                {"name": "From", "value": "gegner@anwaltskanzlei.de"},
                {"name": "To", "value": OWNER},
            ],
            "body": {"data": "RGllIEZyaXN0IGxhZXVmdCBhbSBGcmVpdGFnIGFiLg=="},
        },
    }
    return {
        "GET https://gmail.googleapis.com/gmail/v1/users/me/profile": Recorded(
            {"emailAddress": "Anwalt@Kanzlei.de"}
        ),
        "GET https://gmail.googleapis.com/gmail/v1/users/me/threads": Recorded(
            {"threads": [{"id": "t-1"}]}
        ),
        "GET https://gmail.googleapis.com/gmail/v1/users/me/threads/t-1": Recorded(
            {"id": "t-1", "snippet": "Fristverlängerung", "historyId": "99", "messages": [message]}
        ),
        "GET https://gmail.googleapis.com/gmail/v1/users/me/messages": Recorded(
            {"messages": [{"id": "m-1"}]}
        ),
        "GET https://gmail.googleapis.com/gmail/v1/users/me/messages/m-1": Recorded(message),
    }


def _outlook_mail_routes() -> dict[str, Recorded]:
    return {
        f"GET {GRAPH}/me": Recorded(GRAPH_ME),
        f"GET {GRAPH}/me/mailFolders": Recorded(
            {
                "value": [
                    {
                        "id": "folder-1",
                        "displayName": "Posteingang",
                        "wellKnownName": "inbox",
                        "childFolderCount": 0,
                        "totalItemCount": 1,
                        "unreadItemCount": 0,
                    }
                ]
            }
        ),
        f"GET {GRAPH}/me/mailFolders/delta": Recorded(
            {"value": [], "@odata.deltaLink": f"{GRAPH}/me/mailFolders/delta?token=T2"}
        ),
        f"GET {GRAPH}/me/mailFolders/folder-1/messages": Recorded(
            {
                "value": [
                    {
                        "id": "msg-1",
                        "subject": "Fristverlängerung",
                        "receivedDateTime": "2026-03-01T12:00:00Z",
                        "sentDateTime": "2026-03-01T11:59:00Z",
                        "hasAttachments": False,
                        "from": {"emailAddress": {"address": "gegner@anwaltskanzlei.de"}},
                        "body": {
                            "content": "Die Frist läuft am Freitag ab.",
                            "contentType": "text",
                        },
                    }
                ]
            }
        ),
        f"GET {GRAPH}/me/mailFolders/folder-1/messages/delta": Recorded(
            {
                "value": [],
                "@odata.deltaLink": f"{GRAPH}/me/mailFolders/folder-1/messages/delta?t=T2",
            }
        ),
    }


def _outlook_calendar_routes() -> dict[str, Recorded]:
    return {
        f"GET {GRAPH}/me": Recorded(GRAPH_ME),
        f"GET {GRAPH}/me/calendars": Recorded(
            {"value": [{"id": "cal-1", "name": "Kalender", "isDefaultCalendar": True}]}
        ),
        f"GET {GRAPH}/me/calendars/cal-1/events": Recorded(
            {
                "value": [
                    {
                        "id": "event-1",
                        "subject": "Mandantengespräch Schmidt",
                        "hasAttachments": False,
                        "isCancelled": False,
                        "start": {"dateTime": "2026-03-04T09:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-03-04T10:00:00", "timeZone": "UTC"},
                        "createdDateTime": "2026-02-01T09:00:00Z",
                        "lastModifiedDateTime": "2026-02-02T09:00:00Z",
                        "body": {"content": "Vorbereitung Vergleich.", "contentType": "text"},
                    }
                ]
            }
        ),
    }


def _onenote_routes() -> dict[str, Recorded]:
    return {
        f"GET {GRAPH}/me": Recorded(GRAPH_ME),
        f"GET {GRAPH}/me/onenote/notebooks": Recorded(
            {
                "value": [
                    {
                        "id": "notebook-1",
                        "displayName": "Mandate",
                        "createdDateTime": "2026-01-05T09:00:00Z",
                        "lastModifiedDateTime": "2026-03-01T12:00:00Z",
                        "sections": [
                            {
                                "id": "section-1",
                                "displayName": "Schmidt",
                                "createdDateTime": "2026-01-05T09:00:00Z",
                            }
                        ],
                    }
                ]
            }
        ),
        f"GET {GRAPH}/me/onenote/sections/section-1/pages": Recorded(
            {
                "value": [
                    {
                        "id": "page-1",
                        "title": "Fristenkalender",
                        "contentUrl": f"{GRAPH}/me/onenote/pages/page-1/content",
                        "createdDateTime": "2026-01-06T09:00:00Z",
                        "lastModifiedDateTime": "2026-03-01T12:00:00Z",
                    }
                ]
            }
        ),
        f"GET {GRAPH}/me/onenote/pages/page-1/content": Recorded(
            content=b"<html><body>Frist 06.03.</body></html>"
        ),
    }


OWNER_SCOPED = {
    "gmail": (_gmail_routes, "GET https://gmail.googleapis.com/gmail/v1/users/me/profile"),
    "outlook_mail": (_outlook_mail_routes, f"GET {GRAPH}/me"),
    "outlook_calendar": (_outlook_calendar_routes, f"GET {GRAPH}/me"),
    "onenote": (_onenote_routes, f"GET {GRAPH}/me"),
}


@pytest.mark.parametrize("short_name", sorted(OWNER_SCOPED))
def test_an_owner_scoped_corpus_is_mirrored_to_its_owner(short_name, tmp_path):
    routes_for, _owner_route = OWNER_SCOPED[short_name]
    observations = _scan(short_name, routes_for(), tmp_path)

    assert observations, f"{short_name} produced nothing indexable"
    for item in observations:
        assert item.acl, f"{item.path} carries no ACL, so nobody can retrieve it"
        principals = {grant["principal"] for grant in item.acl}
        assert principals == {f"user:{OWNER}"}, item.path
        # Never firm-wide: publishing one person's mailbox to the firm is the failure this
        # connector's ACL exists to prevent.
        assert "role:authenticated" not in principals
        assert all(grant["effect"] == "allow" for grant in item.acl)


@pytest.mark.parametrize("short_name", sorted(OWNER_SCOPED))
def test_an_owner_scoped_corpus_resolves_its_owner_once_per_run(short_name, tmp_path):
    routes_for, owner_route = OWNER_SCOPED[short_name]
    owner_url = owner_route.split(" ", 1)[1]
    connector, client = build(short_name, routes_for(), staging=tmp_path)
    try:
        list(connector.full_scan())
    finally:
        connector.close()
    # One lookup for the whole run. A call per item would spend the tenant's quota
    # re-learning an address that cannot change mid-sync. Counted on exact equality
    # because /me is a prefix of most of the other endpoints these connectors use.
    exact = [url for _method, url in client.requests if url == owner_url]
    assert len(exact) == 1, f"{short_name} resolved its owner {len(exact)} times"


@pytest.mark.parametrize("short_name", sorted(OWNER_SCOPED))
def test_a_failed_owner_lookup_leaves_access_unknown_but_keeps_the_documents(
    short_name, tmp_path
):
    routes_for, owner_route = OWNER_SCOPED[short_name]
    routes = routes_for()
    routes[owner_route] = Recorded({"error": {"code": "accessDenied"}}, status=403)
    observations = _scan(short_name, routes, tmp_path)

    assert observations, f"a failed owner lookup lost every {short_name} document"
    # Unknown, not "nobody": the engine has to tell a capability gap from a restriction,
    # and absence would let a full scan tombstone real documents.
    assert all(item.acl is None for item in observations)


def test_an_owner_lookup_without_an_address_stays_unknown(tmp_path):
    # Graph can answer /me without a mail or UPN. Inventing a principal from the object id
    # would grant on something nobody authenticates as.
    routes = _outlook_mail_routes()
    routes[f"GET {GRAPH}/me"] = Recorded({"id": "u-1", "displayName": "Anwalt"})
    observations = _scan("outlook_mail", routes, tmp_path)
    assert observations, "the mailbox produced nothing indexable"
    assert all(item.acl is None for item in observations)


# -------------------------------------------------------------------- per-item failures


def test_one_forbidden_item_does_not_abort_the_sharepoint_scan(tmp_path):
    routes = _sharepoint_routes()
    # A single item whose permissions cannot be read must not lose the whole site.
    routes[f"GET {GRAPH}/drives/drive-1/items/item-1/permissions"] = Recorded(
        {"error": {"code": "accessDenied"}}, status=403
    )
    connector, _client = build("sharepoint_online", routes, staging=tmp_path)
    try:
        observations = list(connector.full_scan())
    finally:
        connector.close()
    # The document still arrives; its ACL is unknown, which is fail-closed, not fatal.
    documents = [item for item in observations if item.name.endswith(".docx")]
    assert documents, "a failed permission read lost the document entirely"
    # Unknown, not "nobody": the engine must be able to tell a capability gap from a
    # deliberate restriction. And absence would be worse — a full scan tombstones what it
    # does not see, so a transient 403 would delete real documents.
    assert documents[0].acl is None


def test_the_harness_refuses_to_invent_responses(tmp_path):
    # Guards the harness itself: a silent default would let a connector "pass" while
    # skipping the very call a fixture was meant to exercise.
    client = ReplayClient({f"GET {GRAPH}/me/drive": Recorded({})})
    with pytest.raises(UnexpectedRequest):
        client._match("GET", f"{GRAPH}/me/messages")


# ------------------------------------------------------------------- subtree scoping
#
# A firm does not want its whole drive indexed: the matter folders are the corpus, and
# everything else is cost, dilution and a harder DPO conversation. A selection is
# therefore a set of *subtree roots* — "this folder and everything below it, now and in
# future" — because firms open new matter folders continuously and a flat list would go
# stale the day they do.
#
# Two failures are checked on every one of these connectors, because both are silent and
# both are severe:
#
#   * an empty selection must still enumerate the whole source, or every existing
#     connection quietly changes what it syncs;
#   * a selected folder that has been deleted or unshared must be skipped with a warning
#     and never widen the scan — falling back to "sync everything" would publish a
#     partner's entire drive behind an operator who asked for one matter.


def _root(node_id: str, **metadata) -> NodeSelectionData:
    """One chosen subtree root, as the admin UI stores it."""
    return NodeSelectionData(
        source_node_id=node_id,
        node_type="folder",
        node_title=node_id,
        node_metadata=metadata or None,
    )


def _scoped_scan(
    short_name: str,
    routes: dict,
    tmp_path,
    roots: list | None = None,
    config: dict | None = None,
) -> tuple[set[str], ReplayClient]:
    """Scan with (or without) a selection and return the names produced."""
    connector, client = build(
        short_name, routes, staging=tmp_path, config=config, node_selections=roots
    )
    try:
        return {item.name for item in connector.full_scan()}, client
    finally:
        connector.close()


def _browse(short_name: str, routes: dict, tmp_path, node=None, config=None) -> list[dict]:
    connector, _client = build(short_name, routes, staging=tmp_path, config=config)
    try:
        return connector.browse_children(node)
    finally:
        connector.close()


# ---------------------------------------------------------------------- OneDrive scope


def _onedrive_file(identifier: str, name: str) -> dict:
    return {
        "id": identifier,
        "name": name,
        "size": 14,
        "file": {"mimeType": "text/plain"},
        "createdDateTime": "2026-01-01T00:00:00Z",
        "lastModifiedDateTime": "2026-02-01T00:00:00Z",
        "parentReference": {"driveId": "drive-1"},
        "webUrl": f"https://onedrive.example/{identifier}",
    }


def _onedrive_folder(identifier: str, name: str, child_count: int = 1) -> dict:
    return {
        "id": identifier,
        "name": name,
        "folder": {"childCount": child_count},
        "createdDateTime": "2026-01-01T00:00:00Z",
        "lastModifiedDateTime": "2026-02-01T00:00:00Z",
        "parentReference": {"driveId": "drive-1"},
    }


def _onedrive_routes() -> dict[str, Recorded]:
    """A drive with two top-level folders, one of which has a nested subfolder."""
    return {
        f"GET {GRAPH}/me/drive": Recorded({"id": "drive-1", "name": "OneDrive"}),
        f"GET {GRAPH}/drives/drive-1/root/children": Recorded(
            {
                "value": [
                    _onedrive_folder("f-mandate", "Mandate"),
                    _onedrive_folder("f-privat", "Privat"),
                    _onedrive_file("i-root", "Wurzel.txt"),
                ]
            }
        ),
        f"GET {GRAPH}/drives/drive-1/items/f-mandate/children": Recorded(
            {
                "value": [
                    _onedrive_file("i-mandat", "Schmidt.txt"),
                    # Opened after the scope was chosen: a root has to pick this up.
                    _onedrive_folder("f-neu", "Mandat-Neu"),
                ]
            }
        ),
        f"GET {GRAPH}/drives/drive-1/items/f-neu/children": Recorded(
            {"value": [_onedrive_file("i-neu", "Klageschrift.txt")]}
        ),
        f"GET {GRAPH}/drives/drive-1/items/f-privat/children": Recorded(
            {"value": [_onedrive_file("i-privat", "Steuer.txt")]}
        ),
        f"GET {GRAPH}/drives/drive-1/items/f-weg/children": Recorded(
            {"error": {"code": "itemNotFound", "message": "Item not found"}}, status=404
        ),
        f"GET {GRAPH}/drives/drive-1/items/i- | /content": Recorded(content=b"onedrive bytes"),
        # Minted before the crawl so mid-crawl changes replay on the first delta drain.
        f"GET {GRAPH}/drives/drive-1/root/delta | token=latest": Recorded(
            {"value": [], "@odata.deltaLink": f"{GRAPH}/drives/drive-1/root/delta?token=T1"}
        ),
    }


ONEDRIVE_SCOPE_CONFIG = {"mirror_permissions": False}


def _onedrive_cursor_data() -> dict:
    return {
        "drive_delta_tokens": {"drive-1": f"{GRAPH}/drives/drive-1/root/delta?token=T1"},
        "full_sync_required": False,
        "last_full_sync_timestamp": datetime.now(UTC).isoformat(),
        "synced_drive_ids": {"drive-1": "OneDrive"},
    }


def _onedrive_changes_routes() -> dict[str, Recorded]:
    """The delta feed after T1: a change in each folder plus one deletion."""
    return {
        f"GET {GRAPH}/drives/drive-1/root/delta | token=T1": Recorded(
            {
                "value": [
                    {**_onedrive_folder("drive-1-root", "root"), "root": {}},
                    {
                        **_onedrive_file("i-mandat", "Schmidt.txt"),
                        "parentReference": {"driveId": "drive-1", "id": "f-mandate"},
                    },
                    {
                        **_onedrive_file("i-privat", "Steuer.txt"),
                        "parentReference": {"driveId": "drive-1", "id": "f-privat"},
                    },
                    {"id": "i-weg", "name": "Alt.txt", "deleted": {"state": "deleted"}},
                ],
                "@odata.deltaLink": f"{GRAPH}/drives/drive-1/root/delta?token=T2",
            }
        ),
        # Ancestry of the item outside the selected folder ends at the drive root.
        f"GET {GRAPH}/drives/drive-1/items/f-privat | $select": Recorded(
            {"id": "f-privat", "parentReference": {"id": "drive-1-root"}}
        ),
        f"GET {GRAPH}/drives/drive-1/items/drive-1-root | $select": Recorded(
            {"id": "drive-1-root", "parentReference": {}}
        ),
        f"GET {GRAPH}/drives/drive-1/items/i- | /content": Recorded(content=b"delta bytes"),
    }


def test_onedrive_changes_feed_reports_deltas_deletions_and_mirrored_acls(tmp_path):
    routes = _onedrive_changes_routes()
    for item_id in ("i-mandat", "i-privat"):
        routes[f"GET {GRAPH}/drives/drive-1/items/{item_id}/permissions"] = Recorded(
            {
                "value": [
                    {
                        "id": "p1",
                        "roles": ["read"],
                        "grantedToV2": {"user": {"email": "anwalt@kanzlei.de"}},
                    }
                ]
            }
        )
    connector, _client = build(
        "onedrive", routes, staging=tmp_path, cursor_data=_onedrive_cursor_data()
    )
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    assert {item.name for item in batch.observations} == {"Schmidt.txt", "Steuer.txt"}
    assert batch.deleted_external_ids == ["i-weg"]
    # The refetched permissions travel through the bridge as source-object grants.
    schmidt = next(item for item in batch.observations if item.name == "Schmidt.txt")
    assert [grant["principal"] for grant in schmidt.acl] == ["user:anwalt@kanzlei.de"]
    assert "token=T2" in json.loads(batch.next_cursor)["drive_delta_tokens"]["drive-1"]


def test_onedrive_filters_the_drive_wide_delta_feed_too(tmp_path):
    # The delta feed reports the whole drive, so without filtering here a scoped
    # connection would quietly widen to everything on its second sync.
    connector, _client = build(
        "onedrive",
        _onedrive_changes_routes(),
        staging=tmp_path,
        cursor_data=_onedrive_cursor_data(),
        config=ONEDRIVE_SCOPE_CONFIG,
        node_selections=[_root("f-mandate", drive_id="drive-1", folder_id="f-mandate")],
    )
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    assert {item.name for item in batch.observations} == {"Schmidt.txt"}
    # The item outside the selection is removed rather than indexed.
    assert set(batch.deleted_external_ids) == {"i-privat", "i-weg"}


def test_onedrive_without_a_selection_still_enumerates_the_whole_drive(tmp_path):
    names, _client = _scoped_scan(
        "onedrive", _onedrive_routes(), tmp_path, config=ONEDRIVE_SCOPE_CONFIG
    )
    assert names == {"Wurzel.txt", "Schmidt.txt", "Klageschrift.txt", "Steuer.txt"}


def test_onedrive_scoped_to_a_folder_syncs_only_that_subtree(tmp_path):
    names, client = _scoped_scan(
        "onedrive",
        _onedrive_routes(),
        tmp_path,
        roots=[_root("f-mandate", drive_id="drive-1", folder_id="f-mandate")],
        config=ONEDRIVE_SCOPE_CONFIG,
    )
    # The nested folder was created after the scope was set and is still picked up:
    # a root means the subtree, not a snapshot of it.
    assert names == {"Schmidt.txt", "Klageschrift.txt"}
    # Not merely filtered afterwards — the unselected branches were never asked for.
    assert not client.called("/drives/drive-1/root/children")
    assert not client.called("/items/f-privat/children")
    assert client.call_count("/items/f-mandate/children") == 1


def test_onedrive_skips_a_vanished_root_without_falling_back_to_the_drive(tmp_path):
    names, client = _scoped_scan(
        "onedrive",
        _onedrive_routes(),
        tmp_path,
        roots=[_root("f-mandate"), _root("f-weg")],
        config=ONEDRIVE_SCOPE_CONFIG,
    )
    # The surviving root still syncs...
    assert "Schmidt.txt" in names
    # ...and the drive was not swept up in place of the folder that disappeared.
    assert "Steuer.txt" not in names and "Wurzel.txt" not in names
    assert not client.called("/drives/drive-1/root/children")


def test_onedrive_browse_lists_the_folder_tree_for_the_picker(tmp_path):
    routes = _onedrive_routes()
    top = _browse("onedrive", routes, tmp_path, config=ONEDRIVE_SCOPE_CONFIG)
    assert [node["source_node_id"] for node in top] == ["f-mandate", "f-privat"]
    assert all(node["node_type"] == "folder" for node in top)
    assert all(node["has_children"] for node in top)
    # Files are not roots: a subtree is the unit of selection.
    assert "i-root" not in {node["source_node_id"] for node in top}

    nested = _browse("onedrive", routes, tmp_path, node="f-mandate", config=ONEDRIVE_SCOPE_CONFIG)
    assert [node["title"] for node in nested] == ["Mandat-Neu"]


# ------------------------------------------------------------------------- Clio


def _clio_document(identifier: int, filename: str, matter: dict | None, group: dict | None = None) -> dict:
    return {
        "id": identifier,
        "etag": f"e-{identifier}",
        "name": filename,
        "filename": filename,
        "content_type": "text/plain",
        "size": 9,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-02-01T00:00:00+00:00",
        "deleted_at": None,
        "matter": matter,
        "group": group,
        "document_category": None,
        "parent": {"id": 900, "type": "Folder"},
    }


_CLIO_MATTER_WALL = {"id": 11, "display_number": "2026-0011", "description": "Wall matter",
                     "status": "open", "group": {"id": 5, "name": "Litigation Wall"},
                     "practice_area": {"id": 1, "name": "Litigation"},
                     "client": {"id": 3, "name": "Mandant GmbH"}}
_CLIO_MATTER_OPEN = {"id": 12, "display_number": "2026-0012", "description": "Open matter",
                     "status": "open", "group": None,
                     "practice_area": {"id": 2, "name": "Corporate"},
                     "client": {"id": 4, "name": "Kunde AG"}}


def _clio_routes() -> dict[str, Recorded]:
    return {
        f"GET {CLIO}/matters.json": Recorded(
            {"data": [_CLIO_MATTER_WALL, _CLIO_MATTER_OPEN], "meta": {"paging": {}}}
        ),
        f"GET {CLIO}/documents.json": Recorded(
            {
                "data": [
                    _clio_document(101, "Klageschrift.txt", {"id": 11, "display_number": "2026-0011"}),
                    _clio_document(102, "Vertrag.txt", {"id": 12, "display_number": "2026-0012"}),
                    # Document-level group overrides the (open) matter.
                    _clio_document(
                        103, "Geheim.txt", {"id": 12, "display_number": "2026-0012"},
                        group={"id": 7, "name": "Partners"},
                    ),
                ],
                "meta": {"paging": {}},
            }
        ),
        f"GET {CLIO}/documents/10 | /download.json": Recorded(content=b"clio bytes"),
    }


def test_clio_full_scan_mirrors_group_walls_and_firm_wide_access(tmp_path):
    connector, _client = build("clio", _clio_routes(), staging=tmp_path)
    try:
        observations = list(connector.full_scan())
    finally:
        connector.close()

    by_name = {item.name: item for item in observations}
    assert set(by_name) == {"Klageschrift.txt", "Vertrag.txt", "Geheim.txt"}
    # Restricted matter -> its permission group, nobody else.
    wall = [grant["principal"] for grant in by_name["Klageschrift.txt"].acl]
    assert wall == ["group:clio:5"]
    # Unrestricted matter -> the whole firm (single-firm appliance).
    open_grants = [grant["principal"] for grant in by_name["Vertrag.txt"].acl]
    assert open_grants == ["role:authenticated"]
    # A document-level group wins over its matter's openness.
    doc_grants = [grant["principal"] for grant in by_name["Geheim.txt"].acl]
    assert doc_grants == ["group:clio:7"]
    # Matter context travels as the path prefix.
    assert by_name["Klageschrift.txt"].path.startswith("2026-0011")


def test_clio_scoped_to_a_matter_syncs_only_that_matter(tmp_path):
    routes = _clio_routes()
    routes[f"GET {CLIO}/matters/11.json"] = Recorded({"data": _CLIO_MATTER_WALL})
    routes[f"GET {CLIO}/documents.json | matter_id"] = Recorded(
        {
            "data": [
                _clio_document(101, "Klageschrift.txt", {"id": 11, "display_number": "2026-0011"})
            ],
            "meta": {"paging": {}},
        }
    )
    connector, client = build(
        "clio",
        routes,
        staging=tmp_path,
        node_selections=[
            NodeSelectionData(
                source_node_id="11",
                node_type="folder",
                node_title="2026-0011",
                node_metadata={"matter_id": "11"},
            )
        ],
    )
    try:
        names = {item.name for item in connector.full_scan()}
    finally:
        connector.close()

    assert names == {"Klageschrift.txt"}
    # The estate-wide matter listing was never asked for.
    assert not client.called(f"{CLIO}/matters.json")


def _clio_cursor(**overrides) -> dict:
    data = {
        "updated_since": "2026-02-15T00:00:00+00:00",
        "full_sync_required": False,
        "last_full_sync_timestamp": datetime.now(UTC).isoformat(),
        "matter_groups": {"11": "5"},
        "matter_documents": {"11": ["101"]},
    }
    data.update(overrides)
    return data


def test_clio_changes_feed_reports_edits_deletions_and_moves_out_of_scope(tmp_path):
    changed = _clio_document(101, "Klageschrift.txt", {"id": 11, "display_number": "2026-0011"})
    gone = {**_clio_document(104, "Alt.txt", None), "deleted_at": "2026-03-01T00:00:00+00:00"}
    outside = _clio_document(105, "Privat.txt", {"id": 12, "display_number": "2026-0012"})
    routes = {
        f"GET {CLIO}/documents.json | updated_since": Recorded(
            {"data": [changed, gone, outside], "meta": {"paging": {}}}
        ),
        f"GET {CLIO}/matters/11.json": Recorded({"data": _CLIO_MATTER_WALL}),
        f"GET {CLIO}/documents/10 | /download.json": Recorded(content=b"clio delta bytes"),
    }
    connector, _client = build(
        "clio",
        routes,
        staging=tmp_path,
        cursor_data=_clio_cursor(),
        node_selections=[
            NodeSelectionData(
                source_node_id="11",
                node_type="folder",
                node_title="2026-0011",
                node_metadata={"matter_id": "11"},
            )
        ],
    )
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    assert {item.name for item in batch.observations} == {"Klageschrift.txt"}
    # The source-side deletion and the document outside the selected matter both leave.
    assert set(batch.deleted_external_ids) == {"104", "105"}
    schmidt = next(iter(batch.observations))
    assert [grant["principal"] for grant in schmidt.acl] == ["group:clio:5"]
    assert json.loads(batch.next_cursor)["updated_since"] > "2026-02-15"


def test_clio_incremental_re_emits_a_re_permissioned_matters_documents(tmp_path):
    """A wall is built by changing the matter; no document timestamp moves.

    The incremental run diffs the matter listing against the cursor snapshot and
    re-emits the matter's documents with the new grant — so a permission flip lands
    at the policy interval, not the daily full refresh.
    """
    routes = {
        f"GET {CLIO}/matters/11.json": Recorded({"data": _CLIO_MATTER_WALL}),
        f"GET {CLIO}/documents.json | matter_id": Recorded(
            {
                "data": [
                    _clio_document(
                        101, "Klageschrift.txt", {"id": 11, "display_number": "2026-0011"}
                    )
                ],
                "meta": {"paging": {}},
            }
        ),
        # The change feed itself is quiet: nothing edited, only re-permissioned.
        f"GET {CLIO}/documents.json | updated_since": Recorded(
            {"data": [], "meta": {"paging": {}}}
        ),
        f"GET {CLIO}/documents/10 | /download.json": Recorded(content=b"clio delta bytes"),
    }
    connector, _client = build(
        "clio",
        routes,
        staging=tmp_path,
        # The snapshot remembers the matter as unrestricted; the listing now shows
        # the wall group.
        cursor_data=_clio_cursor(matter_groups={"11": ""}),
        node_selections=[
            NodeSelectionData(
                source_node_id="11",
                node_type="folder",
                node_title="2026-0011",
                node_metadata={"matter_id": "11"},
            )
        ],
    )
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    (observation,) = batch.observations
    assert [grant["principal"] for grant in observation.acl] == ["group:clio:5"]
    assert json.loads(batch.next_cursor)["matter_groups"] == {"11": "5"}


def test_clio_incremental_deletes_a_matter_walled_away_from_the_authorizer(tmp_path):
    """A matter restricted to a group the authorizer is not in simply vanishes.

    Nothing in the change feed says so; the cursor's snapshot is the only record of
    which documents must leave the index with it.
    """
    routes = {
        # The authorizer can no longer see matter 11 at all.
        f"GET {CLIO}/matters/11.json": Recorded(
            {"error": {"type": "NotFound"}}, status=404
        ),
        f"GET {CLIO}/documents.json | updated_since": Recorded(
            {"data": [], "meta": {"paging": {}}}
        ),
    }
    connector, _client = build(
        "clio",
        routes,
        staging=tmp_path,
        cursor_data=_clio_cursor(matter_documents={"11": ["101", "102"]}),
        node_selections=[
            NodeSelectionData(
                source_node_id="11",
                node_type="folder",
                node_title="2026-0011",
                node_metadata={"matter_id": "11"},
            )
        ],
    )
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    assert batch.observations == []
    assert set(batch.deleted_external_ids) == {"101", "102"}
    assert json.loads(batch.next_cursor)["matter_documents"] == {}


def test_clio_expands_permission_groups_into_memberships(tmp_path):
    routes = _clio_routes()
    routes[f"GET {CLIO}/groups/5.json"] = Recorded(
        {
            "data": {
                "id": 5,
                "name": "Litigation Wall",
                "users": [
                    {"id": 21, "email": "Anwalt@Kanzlei.de", "name": "A"},
                    {"id": 22, "email": "referendar@kanzlei.de", "name": "R"},
                ],
            }
        }
    )
    routes[f"GET {CLIO}/groups/7.json"] = Recorded(
        {"data": {"id": 7, "name": "Partners", "users": []}}
    )
    connector, _client = build("clio", routes, staging=tmp_path)
    try:
        list(connector.full_scan())
        memberships = connector.memberships()
    finally:
        connector.close()

    assert {
        (row["member_id"], row["group_id"], row["group_name"]) for row in memberships
    } == {
        ("anwalt@kanzlei.de", "clio:5", "Litigation Wall"),
        ("referendar@kanzlei.de", "clio:5", "Litigation Wall"),
    }


def test_clio_browse_lists_matters_for_the_picker(tmp_path):
    top = _browse("clio", _clio_routes(), tmp_path)
    assert [(node["source_node_id"], node["node_type"]) for node in top] == [
        ("11", "folder"),
        ("12", "folder"),
    ]
    assert top[0]["node_metadata"] == {"matter_id": "11"}
    assert "2026-0011" in top[0]["title"]


# ----------------------------------------------------------------------- Dropbox scope

DROPBOX_LIST = "https://api.dropboxapi.com/2/files/list_folder"


def _dropbox_file(identifier: str, name: str, path: str) -> dict:
    return {
        ".tag": "file",
        "id": identifier,
        "name": name,
        "path_lower": path,
        "path_display": path,
        "size": 13,
        "rev": "0123456789ab",
        "is_downloadable": True,
        "client_modified": "2026-01-01T00:00:00Z",
        "server_modified": "2026-02-01T00:00:00Z",
    }


def _dropbox_folder(identifier: str, name: str, path: str) -> dict:
    return {".tag": "folder", "id": identifier, "name": name, "path_lower": path,
            "path_display": path}


def _dropbox_routes() -> dict[str, Recorded]:
    root_entries = [
        _dropbox_folder("id:mandate", "Mandate", "/mandate"),
        _dropbox_folder("id:privat", "Privat", "/privat"),
        _dropbox_file("id:wurzel", "Wurzel.txt", "/wurzel.txt"),
    ]
    mandate_entries = [
        _dropbox_file("id:schmidt", "Schmidt.txt", "/mandate/schmidt.txt"),
        _dropbox_folder("id:neu", "Mandat-Neu", "/mandate/neu"),
    ]
    neu_entries = [_dropbox_file("id:neu-1", "Klageschrift.txt", "/mandate/neu/klageschrift.txt")]
    return {
        "POST https://api.dropboxapi.com/2/users/get_current_account": Recorded(
            {"account_id": "dbid:1", "name": {"display_name": "Kanzlei"}}
        ),
        f'POST {DROPBOX_LIST} | "path": "", ': Recorded({"entries": root_entries}),
        f'POST {DROPBOX_LIST} | "path": "/mandate", "recursive": false': Recorded(
            {"entries": mandate_entries}
        ),
        # The recursive listing a selected root uses: one call covers the subtree,
        # including folders created since the scope was chosen.
        f'POST {DROPBOX_LIST} | "path": "/mandate", "recursive": true': Recorded(
            {"entries": mandate_entries + neu_entries}
        ),
        f'POST {DROPBOX_LIST} | "path": "/mandate/neu"': Recorded({"entries": neu_entries}),
        f'POST {DROPBOX_LIST} | "path": "/privat"': Recorded(
            {"entries": [_dropbox_file("id:steuer", "Steuer.txt", "/privat/steuer.txt")]}
        ),
        f'POST {DROPBOX_LIST} | "path": "/weg"': Recorded(
            {"error_summary": "path/not_found/"}, status=404
        ),
        "POST https://content.dropboxapi.com/2/files/download": Recorded(
            content=b"dropbox bytes"
        ),
    }


DROPBOX_SCOPE_CONFIG = {"mirror_permissions": False}


def test_dropbox_without_a_selection_still_walks_the_whole_account(tmp_path):
    names, _client = _scoped_scan(
        "dropbox", _dropbox_routes(), tmp_path, config=DROPBOX_SCOPE_CONFIG
    )
    assert {"Wurzel.txt", "Schmidt.txt", "Klageschrift.txt", "Steuer.txt"} <= names


def test_dropbox_scoped_to_a_path_syncs_only_that_subtree(tmp_path):
    names, client = _scoped_scan(
        "dropbox",
        _dropbox_routes(),
        tmp_path,
        roots=[_root("/mandate", path="/mandate")],
        config=DROPBOX_SCOPE_CONFIG,
    )
    assert {"Schmidt.txt", "Klageschrift.txt"} <= names
    assert "Steuer.txt" not in names and "Wurzel.txt" not in names
    # One recursive call instead of a walk, and nothing outside the root was requested.
    assert client.called('"path": "/mandate", "recursive": true')
    assert not client.called('"path": "/privat"')
    assert not client.called('"path": "", ')


def test_dropbox_skips_a_vanished_root_without_falling_back_to_the_account(tmp_path):
    names, client = _scoped_scan(
        "dropbox",
        _dropbox_routes(),
        tmp_path,
        roots=[_root("/mandate"), _root("/weg")],
        config=DROPBOX_SCOPE_CONFIG,
    )
    assert "Schmidt.txt" in names
    assert "Steuer.txt" not in names and "Wurzel.txt" not in names
    assert not client.called('"path": "", ')


def test_dropbox_browse_lists_the_folder_tree_for_the_picker(tmp_path):
    routes = _dropbox_routes()
    top = _browse("dropbox", routes, tmp_path, config=DROPBOX_SCOPE_CONFIG)
    assert [node["source_node_id"] for node in top] == ["/mandate", "/privat"]
    assert all(node["node_type"] == "folder" for node in top)
    assert "/wurzel.txt" not in {node["source_node_id"] for node in top}

    nested = _browse("dropbox", routes, tmp_path, node="/mandate", config=DROPBOX_SCOPE_CONFIG)
    assert [node["source_node_id"] for node in nested] == ["/mandate/neu"]


# --------------------------------------------------------------------------- Box scope

BOX = "https://api.box.com/2.0"


def _box_folder(identifier: str, name: str) -> dict:
    return {
        "id": identifier,
        "type": "folder",
        "name": name,
        "created_at": "2026-01-01T00:00:00-00:00",
        "modified_at": "2026-02-01T00:00:00-00:00",
    }


def _box_file(identifier: str, name: str) -> dict:
    return {
        "id": identifier,
        "type": "file",
        "name": name,
        "size": 11,
        "extension": "txt",
        "created_at": "2026-01-01T00:00:00-00:00",
        "modified_at": "2026-02-01T00:00:00-00:00",
        "permissions": {"can_download": True},
    }


def _box_items(*entries: dict) -> Recorded:
    return Recorded({"entries": list(entries), "total_count": len(entries)})


def _box_routes() -> dict[str, Recorded]:
    return {
        f"GET {BOX}/users/me": Recorded({"id": "1", "name": "Kanzlei"}),
        f"GET {BOX}/users/1": Recorded({"id": "1", "name": "Kanzlei", "login": "a@kanzlei.de"}),
        f"GET {BOX}/folders/0": Recorded(_box_folder("0", "All Files")),
        f"GET {BOX}/folders/0/items": _box_items(
            _box_folder("10", "Mandate"),
            _box_folder("20", "Privat"),
            _box_file("100", "Wurzel.txt"),
        ),
        f"GET {BOX}/folders/10": Recorded(_box_folder("10", "Mandate")),
        f"GET {BOX}/folders/10/items": _box_items(
            _box_file("101", "Schmidt.txt"),
            # Opened after the scope was chosen.
            _box_folder("30", "Mandat-Neu"),
        ),
        f"GET {BOX}/folders/30": Recorded(_box_folder("30", "Mandat-Neu")),
        f"GET {BOX}/folders/30/items": _box_items(_box_file("102", "Klageschrift.txt")),
        f"GET {BOX}/folders/20": Recorded(_box_folder("20", "Privat")),
        f"GET {BOX}/folders/20/items": _box_items(_box_file("103", "Steuer.txt")),
        # 404 on a folder whose grant was withdrawn — Box's helper turns this into an
        # empty body, which is exactly the shape a deleted folder arrives in.
        f"GET {BOX}/folders/99": Recorded({"message": "Not Found"}, status=404),
        f"GET {BOX}/folders/ | /collaborations": Recorded({"entries": []}),
        f"GET {BOX}/files/ | /collaborations": Recorded({"entries": []}),
        f"GET {BOX}/files/ | /comments": Recorded({"entries": []}),
        f"GET {BOX}/files/ | /content": Recorded(content=b"box bytes"),
    }


def test_box_without_a_selection_still_traverses_from_the_root_folder(tmp_path):
    names, _client = _scoped_scan("box", _box_routes(), tmp_path)
    assert {"Wurzel.txt", "Schmidt.txt", "Klageschrift.txt", "Steuer.txt"} <= names


def test_box_scoped_to_a_folder_syncs_only_that_subtree(tmp_path):
    names, client = _scoped_scan(
        "box", _box_routes(), tmp_path, roots=[_root("10", folder_id="10")]
    )
    assert {"Schmidt.txt", "Klageschrift.txt"} <= names
    assert "Steuer.txt" not in names and "Wurzel.txt" not in names
    assert not client.called("/folders/0/items")
    assert not client.called("/folders/20/items")


def test_box_skips_a_vanished_root_without_falling_back_to_folder_zero(tmp_path):
    names, client = _scoped_scan("box", _box_routes(), tmp_path, roots=[_root("10"), _root("99")])
    assert "Schmidt.txt" in names
    assert "Steuer.txt" not in names and "Wurzel.txt" not in names
    assert not client.called("/folders/0/items")


def test_box_browse_lists_the_folder_tree_for_the_picker(tmp_path):
    routes = _box_routes()
    top = _browse("box", routes, tmp_path)
    assert [node["source_node_id"] for node in top] == ["10", "20"]
    assert all(node["node_type"] == "folder" for node in top)
    assert "100" not in {node["source_node_id"] for node in top}

    nested = _browse("box", routes, tmp_path, node="10")
    assert [node["title"] for node in nested] == ["Mandat-Neu"]


# ------------------------------------------------------------------ Google Drive scope

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"


def _drive_file(identifier: str, name: str, parents: list[str]) -> dict:
    return {
        "id": identifier,
        "name": name,
        "mimeType": "text/plain",
        "parents": parents,
        "size": 12,
        "createdTime": "2026-01-05T09:00:00.000Z",
        "modifiedTime": "2026-03-01T12:00:00.000Z",
        "webViewLink": f"https://drive.example/{identifier}",
    }


def _google_drive_routes() -> dict[str, Recorded]:
    """Drive lists drive-wide, so the fixture returns everything and scoping filters."""
    return {
        "GET https://www.googleapis.com/drive/v3/drives": Recorded({"drives": []}),
        "GET https://www.googleapis.com/drive/v3/changes/startPageToken": Recorded(
            {"startPageToken": "T1"}
        ),
        f"GET {DRIVE_FILES}/root-1": Recorded(
            {"id": "root-1", "name": "Mandate", "mimeType": "application/vnd.google-apps.folder"}
        ),
        f"GET {DRIVE_FILES}/weg-1": Recorded({"error": {"message": "File not found"}}, status=404),
        # The ancestry walk: a folder created under the root since the scope was chosen
        # is discovered here, which is what keeps a root live.
        f"GET {DRIVE_FILES} | 'root-1' in parents": Recorded(
            {"files": [{"id": "nested-1", "name": "Mandat-Neu", "parents": ["root-1"]}]}
        ),
        f"GET {DRIVE_FILES} | 'nested-1' in parents": Recorded({"files": []}),
        f"GET {DRIVE_FILES} | 'outside-1' in parents": Recorded({"files": []}),
        f"GET {DRIVE_FILES} | 'root' in parents": Recorded(
            {
                "files": [
                    {"id": "root-1", "name": "Mandate", "parents": ["root"]},
                    {"id": "outside-1", "name": "Privat", "parents": ["root"]},
                ]
            }
        ),
        f"GET {DRIVE_FILES}": Recorded(
            {
                "files": [
                    _drive_file("f-schmidt", "Schmidt.txt", ["root-1"]),
                    _drive_file("f-neu", "Klageschrift.txt", ["nested-1"]),
                    _drive_file("f-privat", "Steuer.txt", ["outside-1"]),
                ]
            }
        ),
        f"GET {DRIVE_FILES}/f-schmidt": Recorded(content=b"drive bytes"),
        f"GET {DRIVE_FILES}/f-neu": Recorded(content=b"drive bytes"),
        f"GET {DRIVE_FILES}/f-privat": Recorded(content=b"drive bytes"),
    }


def _google_drive_changes_routes() -> dict[str, Recorded]:
    routes = _google_drive_routes()
    routes["GET https://www.googleapis.com/drive/v3/changes"] = Recorded(
        {
            "newStartPageToken": "T2",
            "changes": [
                {"fileId": fid, "file": _drive_file(fid, name, [parent])}
                for fid, name, parent in (
                    ("f-schmidt", "Schmidt.txt", "root-1"),
                    ("f-neu", "Klageschrift.txt", "nested-1"),
                    ("f-privat", "Steuer.txt", "outside-1"),
                )
            ],
        }
    )
    return routes


def test_google_drive_without_a_selection_still_lists_the_whole_drive(tmp_path):
    names, client = _scoped_scan("google_drive", _google_drive_routes(), tmp_path)
    assert names == {"Schmidt.txt", "Klageschrift.txt", "Steuer.txt"}
    # No ancestry walk is performed when nothing is scoped.
    assert not client.called("'root-1' in parents")


def test_google_drive_scoped_to_a_folder_filters_by_ancestry(tmp_path):
    names, client = _scoped_scan(
        "google_drive",
        _google_drive_routes(),
        tmp_path,
        roots=[_root("root-1", folder_id="root-1")],
    )
    # Includes the file in the subfolder created after the scope was set.
    assert names == {"Schmidt.txt", "Klageschrift.txt"}
    # The descendant set was resolved once and the unselected branch never expanded.
    assert client.call_count("'root-1' in parents") == 1
    assert not client.called("'outside-1' in parents")
    # Nothing outside the root was downloaded.
    assert not client.called(f"{DRIVE_FILES}/f-privat")


def _google_drive_delta(tmp_path, roots: list | None) -> set[str]:
    """Drive an incremental run off a stored startPageToken."""
    connector, _client = build(
        "google_drive",
        _google_drive_changes_routes(),
        staging=tmp_path,
        cursor_data={"start_page_token": "T1"},
        node_selections=roots,
    )
    try:
        return {item.name for item in connector.changes(None).observations}
    finally:
        connector.close()


def test_google_drive_changes_feed_without_a_selection_reports_the_whole_drive(tmp_path):
    assert _google_drive_delta(tmp_path, None) == {
        "Schmidt.txt",
        "Klageschrift.txt",
        "Steuer.txt",
    }


def test_google_drive_filters_the_drive_wide_changes_feed_too(tmp_path):
    # The changes feed reports the whole drive, so without filtering here a scoped
    # connection would quietly widen to everything on its second sync.
    assert _google_drive_delta(tmp_path, [_root("root-1")]) == {
        "Schmidt.txt",
        "Klageschrift.txt",
    }


def test_google_drive_skips_a_vanished_root_without_falling_back_to_the_drive(tmp_path):
    names, _client = _scoped_scan(
        "google_drive",
        _google_drive_routes(),
        tmp_path,
        roots=[_root("root-1"), _root("weg-1")],
    )
    assert "Schmidt.txt" in names
    assert "Steuer.txt" not in names


def test_google_drive_scoped_to_only_a_vanished_root_syncs_nothing(tmp_path):
    # The failure this whole feature has to avoid: a scope that cannot be resolved must
    # index nothing, never the entire drive.
    names, _client = _scoped_scan(
        "google_drive", _google_drive_routes(), tmp_path, roots=[_root("weg-1")]
    )
    assert names == set()


def test_google_drive_browse_lists_the_folder_tree_for_the_picker(tmp_path):
    routes = _google_drive_routes()
    top = _browse("google_drive", routes, tmp_path)
    assert [node["source_node_id"] for node in top] == ["root-1", "outside-1"]
    assert all(node["node_type"] == "folder" for node in top)

    nested = _browse("google_drive", routes, tmp_path, node="root-1")
    assert [node["title"] for node in nested] == ["Mandat-Neu"]


# ---------------------------------------------------------------- catalog declarations


def test_only_connectors_with_a_folder_tree_declare_scoping():
    scoping = {spec.short_name for spec in CATALOG if spec.supports_scoping}
    # A mailbox, a calendar or a chat workspace has no folder tree worth scoping, and
    # claiming otherwise would offer the operator a picker that cannot be populated.
    # Clio's tree is its matter list — flat, but a real unit of selection.
    assert scoping == {"sharepoint_online", "onedrive", "dropbox", "box", "google_drive", "clio"}


# ----------------------------------------------------------------- SharePoint scope
#
# The reference implementation, kept under the same assertions as the four connectors
# modelled on it — a regression here is as expensive as a regression in any of them.


def _sharepoint_item(identifier: str, name: str) -> dict:
    return {**FILE_ITEM, "id": identifier, "name": name}


def _sharepoint_scope_routes() -> dict[str, Recorded]:
    routes = _sharepoint_routes()
    routes.update(
        {
            f"GET {GRAPH}/drives/drive-1/items/root/children": Recorded(
                {
                    "value": [
                        {"id": "f-mandate", "name": "Mandate", "folder": {"childCount": 2}},
                        {"id": "f-privat", "name": "Privat", "folder": {"childCount": 1}},
                        _sharepoint_item("item-root", "Wurzel.docx"),
                    ]
                }
            ),
            f"GET {GRAPH}/drives/drive-1/items/f-mandate/children": Recorded(
                {
                    "value": [
                        _sharepoint_item("item-1", "Kaufvertrag.docx"),
                        # Opened after the scope was chosen.
                        {"id": "f-neu", "name": "Mandat-Neu", "folder": {"childCount": 1}},
                    ]
                }
            ),
            f"GET {GRAPH}/drives/drive-1/items/f-neu/children": Recorded(
                {"value": [_sharepoint_item("item-neu", "Klageschrift.docx")]}
            ),
            f"GET {GRAPH}/drives/drive-1/items/f-privat/children": Recorded(
                {"value": [_sharepoint_item("item-privat", "Steuer.docx")]}
            ),
            f"GET {GRAPH}/drives/drive-1/items/f-weg/children": Recorded(
                {"error": {"code": "itemNotFound"}}, status=404
            ),
            f"GET {GRAPH}/drives/drive-1/items/item- | /permissions": Recorded(
                SITE_PERMISSIONS
            ),
            f"GET {GRAPH}/drives/drive-1/items/item- | /content": Recorded(
                content=b"PK\x03\x04 docx bytes"
            ),
        }
    )
    return routes


def _sharepoint_folder_root(folder_id: str) -> NodeSelectionData:
    return NodeSelectionData(
        source_node_id=f"folder:drive-1|{folder_id}",
        node_type="folder",
        node_title=folder_id,
        node_metadata={"drive_id": "drive-1", "folder_id": folder_id},
    )


def test_sharepoint_without_a_selection_still_syncs_the_whole_site(tmp_path):
    names, _client = _scoped_scan("sharepoint_online", _sharepoint_scope_routes(), tmp_path)
    assert {"Wurzel.docx", "Kaufvertrag.docx", "Klageschrift.docx", "Steuer.docx"} <= names


def test_sharepoint_scoped_to_a_folder_syncs_only_that_subtree(tmp_path):
    names, client = _scoped_scan(
        "sharepoint_online",
        _sharepoint_scope_routes(),
        tmp_path,
        roots=[_sharepoint_folder_root("f-mandate")],
    )
    assert {"Kaufvertrag.docx", "Klageschrift.docx"} <= names
    assert "Steuer.docx" not in names and "Wurzel.docx" not in names
    assert not client.called("/items/root/children")
    assert not client.called("/items/f-privat/children")


def test_sharepoint_targeted_full_scan_seeds_a_library_delta_cursor(tmp_path):
    connector, client = build(
        "sharepoint_online",
        _sharepoint_scope_routes(),
        staging=tmp_path,
        node_selections=[_sharepoint_folder_root("f-mandate")],
    )
    try:
        names = {item.name for item in connector.full_scan()}
        cursor = json.loads(connector.cursor_state())
    finally:
        connector.close()

    assert {"Kaufvertrag.docx", "Klageschrift.docx"} <= names
    assert cursor["drive_delta_tokens"]["drive-1"].endswith("token=T2")
    assert cursor["full_sync_required"] is False
    assert client.call_count("/drives/drive-1/root/delta") == 1


def test_sharepoint_scoped_delta_tombstones_a_file_moved_out_of_scope(tmp_path):
    moved = {
        **_sharepoint_item("item-moved", "Moved.docx"),
        "parentReference": {
            "driveId": "drive-1",
            "id": "f-privat",
            "path": "/drive/root:/Privat",
        },
    }
    token = f"{GRAPH}/drives/drive-1/root/delta?token=T1"
    routes = _sharepoint_scope_routes()
    routes[f"GET {token}"] = Recorded(
        {
            "value": [moved],
            "@odata.deltaLink": f"{GRAPH}/drives/drive-1/root/delta?token=T2",
        }
    )
    routes[f"GET {GRAPH}/drives/drive-1/items/f-privat"] = Recorded(
        {
            "id": "f-privat",
            "folder": {"childCount": 1},
            "parentReference": {"driveId": "drive-1", "id": "root"},
        }
    )
    connector, client = build(
        "sharepoint_online",
        routes,
        staging=tmp_path,
        cursor_data={
            "drive_delta_tokens": {"drive-1": token},
            "full_sync_required": False,
            "last_full_sync_timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        },
        node_selections=[_sharepoint_folder_root("f-mandate")],
    )
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    assert batch.observations == []
    assert batch.deleted_external_ids == ["spo:file:drive-1:item-moved"]
    assert not client.called("/items/f-mandate/children")
    assert not client.called("/items/f-privat/children")


def test_sharepoint_skips_a_vanished_root_without_falling_back_to_the_site(tmp_path):
    names, client = _scoped_scan(
        "sharepoint_online",
        _sharepoint_scope_routes(),
        tmp_path,
        roots=[_sharepoint_folder_root("f-mandate"), _sharepoint_folder_root("f-weg")],
    )
    assert "Kaufvertrag.docx" in names
    assert "Steuer.docx" not in names and "Wurzel.docx" not in names
    assert not client.called("/items/root/children")


def test_sharepoint_browse_lists_the_folder_tree_for_the_picker(tmp_path):
    routes = _sharepoint_scope_routes()
    sites = _browse("sharepoint_online", routes, tmp_path)
    assert sites and sites[0]["node_type"] == "site"

    drives = _browse("sharepoint_online", routes, tmp_path, node=sites[0]["source_node_id"])
    assert [node["node_type"] for node in drives] == ["drive"]

    folders = _browse("sharepoint_online", routes, tmp_path, node=drives[0]["source_node_id"])
    assert [node["source_node_id"] for node in folders if node["node_type"] == "folder"] == [
        "folder:drive-1|f-mandate",
        "folder:drive-1|f-privat",
    ]

    nested = _browse("sharepoint_online", routes, tmp_path, node="folder:drive-1|f-mandate")
    assert [node["title"] for node in nested if node["node_type"] == "folder"] == ["Mandat-Neu"]
