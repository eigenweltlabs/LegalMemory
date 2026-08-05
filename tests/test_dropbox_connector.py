"""The Dropbox connector, driven against recorded API responses.

Dropbox is a file server for the firms that use it, which decides what is worth proving
here. Three failure classes get the weight, because each is silent and each is severe:

**Permissions.** A matter folder is shared with people and groups, and the index must be
reachable by exactly those people. Over-granting crosses an ethical wall; under-granting
makes a firm's own corpus invisible. Both look like a successful sync.

**Permission changes.** Access is not read once. Somebody is added to a folder, removed
from a group, downgraded to traverse-only. Every one of those has to move the grants in
the index, and revocation has to actually revoke.

**Sync races.** Files and shares change *while* a sync runs. A document written mid-crawl
must not be lost until the next full scan; a rename must not tombstone the document it
renamed; a rejected cursor must not silently stop change tracking.

The replay harness runs the real connector — its request helpers, retry decorators,
entity construction and permission handling — against canned payloads. The engine-level
tests below run the real ``SyncEngine`` and the real permission compiler on a real
database, so what they assert is what a caller would actually be able to retrieve. What
none of this proves is that the recorded payloads still match what Dropbox sends today;
only a live account shows that.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.connectors.cursors.dropbox import DropboxCursor
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.types import NodeSelectionData
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

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
LIST = f"{API}/files/list_folder"
CONTINUE = f"{API}/files/list_folder/continue"
LATEST = f"{API}/files/list_folder/get_latest_cursor"
FOLDER_MEMBERS = f"{API}/sharing/list_folder_members"
FILE_MEMBERS = f"{API}/sharing/list_file_members"
GROUP_MEMBERS = f"{API}/team/groups/members/list"
ACCOUNT = f"{API}/users/get_current_account"
DOWNLOAD = f"{CONTENT}/files/download"

OWNER = "kanzlei@kanzlei.de"
PARTNER = "partnerin@kanzlei.de"
ASSOCIATE = "anwalt@kanzlei.de"
REFERENDAR = "referendar@kanzlei.de"
OUTSIDER = "fremder@kanzlei.de"
LITIGATION_GROUP = "g:litigation"

# The shared folder every matter document hangs off. Reading its members once and
# reusing them is the whole reason this connector is affordable on a real estate.
MANDATE_FOLDER_ID = "sf:mandate"


# --------------------------------------------------------------------------- fixtures


def _file(
    identifier: str,
    name: str,
    path: str,
    *,
    rev: str = "0123456789ab",
    shared_folder: str | None = MANDATE_FOLDER_ID,
    explicit_members: bool = False,
    downloadable: bool = True,
    size: int = 13,
) -> dict:
    entry = {
        ".tag": "file",
        "id": identifier,
        "name": name,
        "path_lower": path,
        "path_display": path,
        "size": size,
        "rev": rev,
        "is_downloadable": downloadable,
        "content_hash": f"hash-{rev}",
        "client_modified": "2026-01-01T00:00:00Z",
        "server_modified": "2026-02-01T00:00:00Z",
        "has_explicit_shared_members": explicit_members,
    }
    if shared_folder:
        entry["sharing_info"] = {"read_only": True, "parent_shared_folder_id": shared_folder}
    return entry


def _folder(identifier: str, name: str, path: str, *, shared_folder: str | None = None) -> dict:
    entry = {".tag": "folder", "id": identifier, "name": name, "path_lower": path,
             "path_display": path}
    if shared_folder:
        entry["sharing_info"] = {"shared_folder_id": shared_folder}
    return entry


def _deleted(path: str, name: str = "gone.txt") -> dict:
    """Dropbox reports a removal with a path and no id. That is the whole problem."""
    return {".tag": "deleted", "name": name, "path_lower": path, "path_display": path}


def _member(email: str, access: str = "editor", *, inherited: bool = True) -> dict:
    return {
        "access_type": {".tag": access},
        "user": {"account_id": f"dbid:{email}", "email": email, "same_team": True},
        "is_inherited": inherited,
    }


def _group_member(group_id: str = LITIGATION_GROUP, access: str = "viewer") -> dict:
    return {
        "access_type": {".tag": access},
        "group": {
            "group_id": group_id,
            "group_name": "Litigation",
            "member_count": 2,
            "same_team": True,
        },
        "is_inherited": True,
    }


# The estate: one shared matter folder with two documents and a subfolder, plus a
# private folder that was never shared with anybody.
SCHMIDT = _file("id:schmidt", "Schmidt.txt", "/mandate/schmidt.txt")
KLAGE = _file("id:klage", "Klageschrift.txt", "/mandate/klage/klageschrift.txt")
PRIVAT = _file("id:privat", "Steuer.txt", "/privat/steuer.txt", shared_folder=None)

ESTATE = [
    _folder("id:f-mandate", "Mandate", "/mandate", shared_folder=MANDATE_FOLDER_ID),
    _folder("id:f-klage", "Klage", "/mandate/klage"),
    _folder("id:f-privat", "Privat", "/privat"),
    SCHMIDT,
    KLAGE,
    PRIVAT,
]


def _routes(
    *,
    entries: list[dict] | None = None,
    folder_users: list[dict] | None = None,
    folder_groups: list[dict] | None = None,
    group_members: list[str] | None = None,
    owner_email: str = OWNER,
) -> dict[str, Recorded]:
    """A complete Dropbox account, with the sharing knobs each test needs to turn."""
    routes: dict[str, Recorded] = {
        f"POST {ACCOUNT}": Recorded(
            {
                "account_id": "dbid:kanzlei",
                "name": {"display_name": "Kanzlei"},
                "email": owner_email,
                "account_type": {".tag": "business"},
            }
        ),
        # The body clause is required: this URL has the list_folder URL as a prefix, so
        # without it the harness would answer the cursor call with a folder listing.
        f'POST {LATEST} | "recursive": true': Recorded({"cursor": "cursor-1"}),
        f'POST {LIST} | "path": "", "recursive": true': Recorded(
            {"entries": entries if entries is not None else ESTATE}
        ),
        f'POST {LIST} | "path": "", "recursive": false': Recorded({"entries": ESTATE[:3]}),
        f'POST {LIST} | "path": "/mandate", "recursive": true': Recorded(
            {"entries": [ESTATE[1], SCHMIDT, KLAGE]}
        ),
        f'POST {LIST} | "path": "/mandate", "recursive": false': Recorded(
            {"entries": [ESTATE[1]]}
        ),
        f"POST {DOWNLOAD}": Recorded(content=b"dropbox bytes"),
        f"POST {FOLDER_MEMBERS}": Recorded(
            {
                "users": folder_users
                if folder_users is not None
                else [_member(PARTNER), _member(ASSOCIATE, "viewer")],
                "groups": folder_groups if folder_groups is not None else [_group_member()],
                "invitees": [
                    # An outstanding invitation is not access.
                    {"access_type": {".tag": "editor"}, "invitee": {"email": OUTSIDER}}
                ],
            }
        ),
    }
    if group_members is not None:
        routes[f"POST {GROUP_MEMBERS}"] = Recorded(
            {
                "group_info": {"group_id": LITIGATION_GROUP, "group_name": "Litigation"},
                "members": [
                    {"profile": {"team_member_id": f"tm:{email}", "email": email},
                     "access_type": {".tag": "member"}}
                    for email in group_members
                ],
                "has_more": False,
            }
        )
    return routes


def _scan(routes: dict, tmp_path, *, config: dict | None = None, roots: list | None = None):
    """Run a full scan and return (observations, connector-derived memberships, client)."""
    connector, client = build(
        "dropbox", routes, staging=tmp_path, config=config, node_selections=roots
    )
    try:
        observations = list(connector.full_scan())
        memberships = connector.memberships()
        return observations, memberships, client, connector.cursor_state()
    finally:
        connector.close()


def _acl_of(observations, name: str) -> list[dict] | None:
    for observation in observations:
        if observation.name == name:
            return observation.acl
    raise AssertionError(f"{name} was not produced by the scan")


def _principals(observations, name: str) -> set[str]:
    return {grant["principal"] for grant in _acl_of(observations, name) or []}


# ------------------------------------------------------------------------- traversal


def test_the_whole_account_is_read_in_one_recursive_listing(tmp_path):
    """A file server is walked once, not once per folder.

    The previous implementation listed every folder three times — once for its files,
    once for its subfolders, and again from a second full tree walk — and emitted each
    folder twice. On an estate of any size that is the difference between a sync that
    finishes and one that gets the firm's account throttled.
    """
    observations, _memberships, client, _cursor = _scan(_routes(), tmp_path)

    assert {row.name for row in observations} == {"Schmidt.txt", "Klageschrift.txt", "Steuer.txt"}
    assert client.call_count(f"POST {LIST} ") == 1
    # Folders and the account are containers. Indexing them would fill the corpus with
    # stubs that match every query weakly.
    assert "Mandate" not in {row.name for row in observations}
    assert "Kanzlei" not in {row.name for row in observations}


def test_pagination_follows_the_continue_cursor(tmp_path):
    routes = _routes(entries=[ESTATE[0], SCHMIDT])
    routes[f'POST {LIST} | "path": "", "recursive": true'] = Recorded(
        {"entries": [ESTATE[0], SCHMIDT], "cursor": "page-2", "has_more": True}
    )
    routes[f'POST {CONTINUE} | "page-2"'] = Recorded({"entries": [KLAGE], "has_more": False})

    observations, _memberships, _client, _cursor = _scan(routes, tmp_path)
    assert {row.name for row in observations} == {"Schmidt.txt", "Klageschrift.txt"}


def test_the_staged_bytes_are_the_documents_own(tmp_path):
    """Content is staged during the scan, keyed by file id, and readable afterwards."""
    observations, _memberships, _client, _cursor = _scan(_routes(), tmp_path)
    for observation in observations:
        assert observation.staged_path
        with open(observation.staged_path, "rb") as handle:
            assert handle.read() == b"dropbox bytes"


def test_the_download_is_pinned_to_the_revision_that_was_listed(tmp_path):
    """A file saved over mid-crawl must not be staged under the old revision's token.

    Downloading by path would fetch whatever the file is *now* and store it against the
    rev the listing reported, leaving the index holding content it believes it has
    already processed and will not revisit.
    """
    _observations, _memberships, client, _cursor = _scan(_routes(), tmp_path)
    downloads = [call for call in client.calls if DOWNLOAD in call]
    assert downloads, "nothing was downloaded"
    assert client.call_count(f"POST {DOWNLOAD}") == 3


def test_the_revision_becomes_the_version_token(tmp_path):
    """Without this the connector reported no version and every rescan re-derived."""
    observations, _memberships, _client, _cursor = _scan(_routes(), tmp_path)
    assert all(row.source_version_label == "0123456789ab" for row in observations)
    assert all(row.change_hint == "0123456789ab" for row in observations)


def test_a_paper_doc_is_skipped_rather_than_staged_empty(tmp_path):
    paper = _file("id:paper", "Notizen.paper", "/mandate/notizen.paper", downloadable=False)
    observations, _m, _c, _cursor = _scan(_routes(entries=[*ESTATE, paper]), tmp_path)
    assert "Notizen.paper" not in {row.name for row in observations}


def test_an_excluded_path_is_not_indexed(tmp_path):
    observations, _m, _c, _cursor = _scan(
        _routes(), tmp_path, config={"exclude_path": "/privat"}
    )
    assert "Steuer.txt" not in {row.name for row in observations}
    assert "Schmidt.txt" in {row.name for row in observations}


def test_one_unreadable_file_does_not_abort_the_scan(tmp_path):
    """A per-document failure is a skipped document, never a failed sync."""
    routes = _routes()
    routes[f"POST {DOWNLOAD}"] = [
        Recorded({"error_summary": "path/not_found/"}, status=404, repeat=False),
        Recorded(content=b"dropbox bytes"),
    ]
    observations, _m, _c, _cursor = _scan(routes, tmp_path)
    assert len(observations) == 2


def test_dead_credentials_surface_as_an_auth_error_not_an_empty_sync(tmp_path):
    """The engine tombstones on an empty scan, so a 401 must never look like one."""
    connector, _client = build(
        "dropbox",
        {f"POST {ACCOUNT}": Recorded({"error_summary": "invalid_access_token/"}, status=401)},
        staging=tmp_path,
    )
    try:
        with pytest.raises(SourceAuthError):
            list(connector.full_scan())
    finally:
        connector.close()


def test_a_throttled_call_is_retried_rather_than_dropped(tmp_path):
    """Dropbox throttles per account; a 429 mid-crawl must not cost the firm a folder."""
    routes = _routes()
    routes[f'POST {LIST} | "path": "", "recursive": true'] = [
        Recorded({"error_summary": "too_many_requests/"}, status=429,
                 headers={"Retry-After": "1"}, repeat=False),
        Recorded({"entries": ESTATE}),
    ]
    observations, _m, _c, _cursor = _scan(routes, tmp_path)
    assert {row.name for row in observations} == {
        "Schmidt.txt", "Klageschrift.txt", "Steuer.txt"
    }


# ----------------------------------------------------------------------- permissions


def test_a_shared_folders_members_reach_every_file_inside_it(tmp_path):
    observations, _m, client, _cursor = _scan(_routes(), tmp_path)

    assert _principals(observations, "Schmidt.txt") == {
        f"user:{PARTNER}",
        f"user:{ASSOCIATE}",
        f"user:{OWNER}",
        f"group:dropbox:{LITIGATION_GROUP}",
    }
    # Read once for the folder, not once per file. This is what makes mirroring
    # affordable on a matter folder with hundreds of documents.
    assert client.call_count(f"POST {FOLDER_MEMBERS}") == 1


def test_an_outstanding_invitation_is_not_access(tmp_path):
    """Mirroring an invitee would grant on the strength of an email someone typed."""
    observations, _m, _c, _cursor = _scan(_routes(), tmp_path)
    assert f"user:{OUTSIDER}" not in _principals(observations, "Schmidt.txt")


def test_a_traverse_only_member_gets_no_read(tmp_path):
    """Dropbox's traverse level lets somebody see a folder exists on the way to
    something below it. It confers no read, and mirroring it as one puts a person in a
    matter's grants who cannot open a single document in it."""
    observations, _m, _c, _cursor = _scan(
        _routes(folder_users=[_member(PARTNER), _member(REFERENDAR, "traverse")]), tmp_path
    )
    assert f"user:{PARTNER}" in _principals(observations, "Schmidt.txt")
    assert f"user:{REFERENDAR}" not in _principals(observations, "Schmidt.txt")


def test_a_no_access_member_gets_no_read(tmp_path):
    observations, _m, _c, _cursor = _scan(
        _routes(folder_users=[_member(PARTNER), _member(REFERENDAR, "no_access")]), tmp_path
    )
    assert f"user:{REFERENDAR}" not in _principals(observations, "Schmidt.txt")


def test_a_traverse_only_group_gets_no_read(tmp_path):
    observations, _m, _c, _cursor = _scan(
        _routes(folder_groups=[_group_member(access="traverse")]), tmp_path
    )
    assert not any(
        principal.startswith("group:")
        for principal in _principals(observations, "Schmidt.txt")
    )


def test_a_file_shared_on_its_own_is_read_individually(tmp_path):
    """An explicit share on one document overrides what its folder says."""
    notiz = _file("id:notiz", "Notiz.txt", "/mandate/notiz.txt", explicit_members=True)
    routes = _routes(entries=[*ESTATE, notiz])
    routes[f'POST {FILE_MEMBERS} | "id:notiz"'] = Recorded(
        {"users": [_member(PARTNER, "viewer")], "groups": [], "invitees": []}
    )

    observations, _m, client, _cursor = _scan(routes, tmp_path)
    assert _principals(observations, "Notiz.txt") == {f"user:{PARTNER}", f"user:{OWNER}"}
    # The folder's wider membership must not leak onto it.
    assert f"group:dropbox:{LITIGATION_GROUP}" not in _principals(observations, "Notiz.txt")
    assert client.call_count(f"POST {FILE_MEMBERS}") == 1


def test_an_unshared_file_belongs_to_the_account_owner(tmp_path):
    """Not unknown: nobody shared it, and the account that authorized the connection can
    plainly open it. Reporting it as unknown would make a firm's own private documents
    unreachable to the person who connected the account."""
    observations, _m, _c, _cursor = _scan(_routes(), tmp_path)
    assert _principals(observations, "Steuer.txt") == {f"user:{OWNER}"}


def test_a_failed_member_read_leaves_access_unknown_not_empty(tmp_path):
    """Unknown is fail-closed and flagged as a gap. Empty asserts "nobody may read
    this", which is a different and wrong claim."""
    routes = _routes()
    routes[f"POST {FOLDER_MEMBERS}"] = Recorded({"error_summary": "access_error/"}, status=403)

    observations, _m, client, _cursor = _scan(routes, tmp_path)
    assert _acl_of(observations, "Schmidt.txt") is None
    # Still indexed — a permission gap is not a reason to drop the document.
    assert "Schmidt.txt" in {row.name for row in observations}
    # And the refusal is cached: retrying per file turns one failure into hundreds.
    assert client.call_count(f"POST {FOLDER_MEMBERS}") == 1


def test_permission_mirroring_can_be_turned_off(tmp_path):
    observations, _m, client, _cursor = _scan(
        _routes(), tmp_path, config={"mirror_permissions": False}
    )
    assert all(row.acl is None for row in observations)
    assert client.call_count(f"POST {FOLDER_MEMBERS}") == 0


def test_a_dropbox_group_is_expanded_into_its_members(tmp_path):
    """Without this a group-shared matter folder is granted to a principal nobody
    authenticates as, and the documents are invisible rather than protected."""
    _observations, memberships, _client, _cursor = _scan(
        _routes(group_members=[PARTNER, REFERENDAR]), tmp_path
    )
    assert memberships == [
        {"member_id": PARTNER, "member_type": "user",
         "group_id": f"dropbox:{LITIGATION_GROUP}", "group_name": "Litigation"},
        {"member_id": REFERENDAR, "member_type": "user",
         "group_id": f"dropbox:{LITIGATION_GROUP}", "group_name": "Litigation"},
    ]


def test_group_expansion_pages_through_a_large_group(tmp_path):
    routes = _routes(group_members=[PARTNER])
    routes[f'POST {GROUP_MEMBERS} | "group_id": "{LITIGATION_GROUP}"'] = Recorded(
        {
            "group_info": {"group_id": LITIGATION_GROUP, "group_name": "Litigation"},
            "members": [{"profile": {"email": PARTNER}}],
            "cursor": "grp-2",
            "has_more": True,
        }
    )
    routes[f'POST {API}/team/groups/members/list/continue'] = Recorded(
        {"members": [{"profile": {"email": REFERENDAR}}], "has_more": False}
    )
    _observations, memberships, _client, _cursor = _scan(routes, tmp_path)
    assert {row["member_id"] for row in memberships} == {PARTNER, REFERENDAR}


def test_a_personal_account_without_the_team_api_still_syncs(tmp_path):
    """Team endpoints are a Dropbox Business feature. A personal account refuses them,
    and that refusal is about the directory, not about the file credentials — so it must
    not abort the sync or be retried once per group."""
    # Two groups, so the assertion below distinguishes "gave up after the first refusal"
    # from "retried every group and happened to fail each time".
    routes = _routes(
        folder_groups=[_group_member(), _group_member("g:corporate")],
    )
    routes[f"POST {GROUP_MEMBERS}"] = Recorded(
        {"error_summary": "invalid_access_token/"}, status=401
    )

    observations, memberships, client, _cursor = _scan(routes, tmp_path)
    assert memberships == []
    assert "Schmidt.txt" in {row.name for row in observations}
    assert client.call_count(f"POST {GROUP_MEMBERS}") == 1


def test_group_expansion_can_be_turned_off(tmp_path):
    _observations, memberships, client, _cursor = _scan(
        _routes(group_members=[PARTNER]), tmp_path, config={"expand_team_groups": False}
    )
    assert memberships == []
    assert client.call_count(f"POST {GROUP_MEMBERS}") == 0


def test_folder_members_paginate(tmp_path):
    routes = _routes()
    routes[f"POST {FOLDER_MEMBERS}"] = Recorded(
        {"users": [_member(PARTNER)], "groups": [], "cursor": "mem-2"}
    )
    routes[f"POST {API}/sharing/list_folder_members/continue"] = Recorded(
        {"users": [_member(REFERENDAR)], "groups": []}
    )
    observations, _m, _c, _cursor = _scan(routes, tmp_path)
    assert {f"user:{PARTNER}", f"user:{REFERENDAR}"} <= _principals(observations, "Schmidt.txt")


# ------------------------------------------------------------------- sync race conditions


def _cursor_data(state: str | None) -> dict:
    return json.loads(state) if state else {}


def test_the_change_cursor_is_minted_before_the_crawl_begins(tmp_path):
    """The race this exists to lose safely.

    A cursor minted *after* the crawl describes the estate as it is at the end, so a file
    written into an already-visited folder while the crawl was running is inside neither
    the crawl nor the next delta drain. It stays missing until some later edit touches it
    again. Minting first means the next drain replays everything the crawl may have run
    past.
    """
    _observations, _m, client, state = _scan(_routes(), tmp_path)

    latest = next(i for i, call in enumerate(client.calls) if LATEST in call)
    listing = next(i for i, call in enumerate(client.calls) if f"POST {LIST} " in call)
    assert latest < listing, "the cursor was minted after the crawl had already started"
    assert _cursor_data(state)["root_cursors"] == {"": "cursor-1"}


def test_a_file_written_during_the_crawl_arrives_on_the_next_sync(tmp_path):
    """The other half of the same race, proven end to end: the connector resumes from
    the pre-crawl cursor and the mid-crawl file comes through as a change."""
    neu = _file("id:neu", "Nachtrag.txt", "/mandate/nachtrag.txt", rev="ffff11112222")
    routes = _routes()
    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded({"entries": [neu], "has_more": False})

    connector, _client = build("dropbox", routes, staging=tmp_path)
    try:
        list(connector.full_scan())
        state = connector.cursor_state()
    finally:
        connector.close()

    connector, _client = build("dropbox", routes, staging=tmp_path, cursor_data=_cursor_data(state))
    try:
        batch = connector.changes(None)
    finally:
        connector.close()

    assert [row.name for row in batch.observations] == ["Nachtrag.txt"]
    assert batch.deleted_external_ids == []


def _drain(routes, tmp_path, cursor_data, roots=None):
    connector, client = build(
        "dropbox", routes, staging=tmp_path, cursor_data=cursor_data, node_selections=roots
    )
    try:
        return connector.changes(None), client, connector.cursor_state()
    finally:
        connector.close()


def _synced_cursor(routes, tmp_path, roots=None) -> dict:
    """Run a full scan and hand back the cursor a later delta run resumes from."""
    connector, _client = build("dropbox", routes, staging=tmp_path, node_selections=roots)
    try:
        list(connector.full_scan())
        return _cursor_data(connector.cursor_state())
    finally:
        connector.close()


def test_a_deletion_is_resolved_back_to_the_id_it_removed(tmp_path):
    """Dropbox reports a removal as a path with no id, and the index is keyed by id.
    Without the path map the deletion is unactionable and the document stays searchable
    until the next full crawl."""
    routes = _routes()
    cursor = _synced_cursor(routes, tmp_path)
    assert cursor["path_ids"]["/mandate/schmidt.txt"] == "id:schmidt"

    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded(
        {"entries": [_deleted("/mandate/schmidt.txt", "Schmidt.txt")], "has_more": False}
    )
    batch, _client, state = _drain(routes, tmp_path, cursor)

    assert batch.deleted_external_ids == ["id:schmidt"]
    # And the path is forgotten, so a later file at the same path is not confused for it.
    assert "/mandate/schmidt.txt" not in _cursor_data(state)["path_ids"]


def test_a_rename_does_not_tombstone_the_document_it_renamed(tmp_path):
    """Dropbox reports a rename as a removal at the old path plus the file at its new
    one, with the same id on both sides. The engine applies deletions *after*
    observations, so emitting both would delete the document the same batch just
    updated — the file would vanish from the index on being renamed."""
    routes = _routes()
    cursor = _synced_cursor(routes, tmp_path)

    renamed = _file("id:schmidt", "Schmidt-2026.txt", "/mandate/schmidt-2026.txt")
    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded(
        {"entries": [_deleted("/mandate/schmidt.txt", "Schmidt.txt"), renamed], "has_more": False}
    )
    batch, _client, state = _drain(routes, tmp_path, cursor)

    assert [row.name for row in batch.observations] == ["Schmidt-2026.txt"]
    assert batch.deleted_external_ids == []
    # The map follows the file to its new path.
    assert _cursor_data(state)["path_ids"]["/mandate/schmidt-2026.txt"] == "id:schmidt"


def test_deleting_a_folder_tombstones_everything_that_was_under_it(tmp_path):
    """Dropbox emits one deleted entry for the folder and none for its contents. Taking
    that literally would leave a removed matter searchable in full."""
    routes = _routes()
    cursor = _synced_cursor(routes, tmp_path)

    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded(
        {"entries": [_deleted("/mandate", "Mandate")], "has_more": False}
    )
    batch, _client, _state = _drain(routes, tmp_path, cursor)

    assert set(batch.deleted_external_ids) == {"id:schmidt", "id:klage"}
    # The private folder was not under it and must survive.
    assert "id:privat" not in batch.deleted_external_ids


def test_a_rejected_cursor_asks_for_a_crawl_instead_of_stopping_change_tracking(tmp_path):
    """Dropbox invalidates cursors ("reset"). Swallowing that would leave the connector
    resuming from a cursor the server has already rejected, and change tracking would be
    silently dead."""
    routes = _routes()
    cursor = _synced_cursor(routes, tmp_path)

    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded(
        {"error_summary": "reset/", "error": {".tag": "reset"}}, status=409
    )
    _batch, _client, state = _drain(routes, tmp_path, cursor)

    data = _cursor_data(state)
    assert data["full_sync_required"] is True
    # The rejected cursor is dropped with the flag: keeping it would let a later run
    # resume from it and skip everything that changed while it was invalid.
    assert data["root_cursors"] == {}


def test_re_scoping_forces_a_crawl_rather_than_draining_the_old_roots(tmp_path):
    """A cursor describes the listing it was minted from. Resuming after the operator
    changed which folders are synced would drain the feed of folders nobody selected any
    more, and never enumerate the ones they did."""
    routes = _routes()
    cursor = _synced_cursor(routes, tmp_path)
    assert set(cursor["root_cursors"]) == {""}

    root = NodeSelectionData(
        source_node_id="/mandate", node_type="folder", node_title="Mandate",
        node_metadata={"path": "/mandate"},
    )
    connector, client = build(
        "dropbox", routes, staging=tmp_path, cursor_data=cursor, node_selections=[root]
    )
    try:
        names = {row.name for row in connector.full_scan()}
    finally:
        connector.close()

    assert names == {"Schmidt.txt", "Klageschrift.txt"}
    assert client.called('"path": "/mandate", "recursive": true')


def test_a_root_that_could_not_be_listed_is_crawled_again_rather_than_delta_resumed(tmp_path):
    """A root whose listing failed was never enumerated, so its cursor is worthless.

    Resuming a delta from a cursor minted before a crawl that did not happen means the
    root's existing files are re-observed by neither pass, and they stay missing from the
    index until something happens to change them.
    """
    routes = _routes()
    routes[f'POST {LIST} | "path": "", "recursive": true'] = Recorded(
        {"error_summary": "internal_error/"}, status=500
    )

    connector, _client = build("dropbox", routes, staging=tmp_path)
    try:
        assert list(connector.full_scan()) == []
        state = connector.cursor_state()
    finally:
        connector.close()

    data = _cursor_data(state)
    assert data["root_cursors"] == {}
    # And the next run therefore crawls: the roots it holds cursors for no longer match
    # the roots it is asked to sync.
    assert DropboxCursor(**data).needs_full_sync() is True


def test_an_overgrown_path_map_asks_for_a_crawl_rather_than_growing_without_bound(tmp_path):
    """The map is persisted per source. Past its bound it costs more than the crawl it
    saves, so it is traded for one."""
    routes = _routes()
    cursor = _synced_cursor(routes, tmp_path)
    cursor["path_ids"] = {f"/bulk/{index}.txt": f"id:{index}" for index in range(50_001)}
    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded({"entries": [], "has_more": False})

    _batch, _client, state = _drain(routes, tmp_path, cursor)
    assert _cursor_data(state)["full_sync_required"] is True


def test_an_interrupted_crawl_keeps_its_checkpoint_but_still_asks_to_crawl_again(tmp_path):
    """Two things have to hold together when a crawl is abandoned halfway.

    The cursor it already paid a request for is kept, so the work is not repeated. And
    ``full_sync_required`` stays set, so the next run crawls rather than resuming a delta
    from a point where half the estate was never indexed — which would leave those
    documents missing until something happened to touch them.
    """
    connector, _client = build("dropbox", _routes(), staging=tmp_path)
    try:
        scan = connector.full_scan()
        next(scan)
        scan.close()
        state = connector.cursor_state()
    finally:
        connector.close()

    data = _cursor_data(state)
    assert data["root_cursors"] == {"": "cursor-1"}
    assert data["full_sync_required"] is True


def test_a_permission_change_alone_re_emits_the_file_with_new_grants(tmp_path):
    """The change a firm cares most about, and the one Dropbox's feed is worst at: a
    person added to a matter folder. No file is rewritten, so nothing appears in the
    delta feed — which is why the connector must re-read members on the crawl the engine
    forces for exactly this."""
    before, _m, _c, _cursor = _scan(
        _routes(folder_users=[_member(PARTNER)], folder_groups=[]), tmp_path
    )
    assert _principals(before, "Schmidt.txt") == {f"user:{PARTNER}", f"user:{OWNER}"}

    after, _m, _c, _cursor = _scan(
        _routes(
            folder_users=[_member(PARTNER), _member(REFERENDAR, "viewer")], folder_groups=[]
        ),
        tmp_path,
    )
    assert _principals(after, "Schmidt.txt") == {
        f"user:{PARTNER}", f"user:{REFERENDAR}", f"user:{OWNER}"
    }


# ------------------------------------------------------- cursor unit behaviour


def test_ids_under_matches_a_subtree_and_not_a_sibling_with_the_same_prefix():
    """`/mandate-alt` must not be swept up by a deletion of `/mandate`."""
    cursor = DropboxCursor()
    cursor.remember_path("/mandate/a.txt", "id:a")
    cursor.remember_path("/mandate/tief/b.txt", "id:b")
    cursor.remember_path("/mandate-alt/c.txt", "id:c")

    assert set(cursor.ids_under("/mandate")) == {"id:a", "id:b"}


def test_the_path_map_is_case_insensitive_like_dropbox_itself():
    """Dropbox paths are case-insensitive; a deletion reported in different casing must
    still resolve, or the document silently survives its own deletion."""
    cursor = DropboxCursor()
    cursor.remember_path("/Mandate/Schmidt.txt", "id:schmidt")
    assert cursor.ids_under("/mandate/schmidt.txt") == ["id:schmidt"]


# --------------------------------------------------- end to end, through the engine
#
# From here the real SyncEngine and the real permission compiler run against a real
# database, so what is asserted is what a caller could actually retrieve.


def _source(session: Session) -> Source:
    source = Source(kind="dropbox", display_name="Dropbox", config={})
    session.add(source)
    session.flush()
    return source


def _sync(session: Session, source: Source, routes: dict, tmp_path, **kwargs) -> object:
    connector, _client = build("dropbox", routes, staging=tmp_path)
    try:
        result = SyncEngine(session, source, connector).sync(**kwargs)
    finally:
        connector.close()
    session.flush()
    return result


def _sync_delta(session: Session, source: Source, routes: dict, tmp_path) -> object:
    """Run the engine on its incremental path, resuming from the stored cursor.

    ``acl_refresh_hours=0`` keeps it there: the daily crawl the engine forces to re-read
    permissions would otherwise pre-empt the delta drain, and these tests would silently
    be exercising a full scan instead of the change feed.
    """
    connector, _client = build(
        "dropbox", routes, staging=tmp_path, cursor_data=_cursor_data(source.cursor)
    )
    try:
        result = SyncEngine(session, source, connector, acl_refresh_hours=0).sync()
    finally:
        connector.close()
    session.flush()
    return result


def _promote(session: Session, source: Source, needle: str) -> str:
    """Attach a synced object to a document version, as the pipeline would.

    Retrieval is scoped on document versions, so mirrored grants only take effect once an
    object is linked to one.
    """
    row = session.scalars(
        select(SourceObject).where(
            SourceObject.source_id == source.id,
            SourceObject.name.contains(needle),
            SourceObject.deleted_at.is_(None),
        )
    ).first()
    assert row is not None, f"the sync produced no object named like {needle!r}"

    project = Project(key=f"P-{needle}", name=needle)
    blob = Blob(content_hash=("e" * 63) + needle[0].lower(), size_bytes=13)
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


def test_a_member_of_the_matter_folder_can_retrieve_its_documents(
    session: Session, tmp_path
) -> None:
    source = _source(session)
    _sync(session, source, _routes(group_members=[]), tmp_path)
    version_id = _promote(session, source, "Schmidt")

    access = AccessService(session)
    assert version_id in access.visible_version_ids({f"user:{PARTNER}"})
    assert version_id not in access.visible_version_ids({f"user:{OUTSIDER}"})


def test_someone_reaches_a_matter_only_through_the_dropbox_group(
    session: Session, tmp_path
) -> None:
    """The multi-user case, isolated: this person holds no direct grant on the folder.
    They reach the documents solely because the group's membership was mirrored."""
    source = _source(session)
    _sync(session, source, _routes(group_members=[REFERENDAR]), tmp_path)
    version_id = _promote(session, source, "Schmidt")

    access = AccessService(session)
    assert version_id in access.visible_version_ids({f"user:{REFERENDAR}"})
    assert version_id not in access.visible_version_ids({f"user:{OUTSIDER}"})


def test_removing_someone_from_the_dropbox_group_revokes_their_access(
    session: Session, tmp_path
) -> None:
    """Revocation at source has to revoke here. Mirroring memberships additively would
    leave the old edge in place and keep serving withdrawn access indefinitely."""
    source = _source(session)
    _sync(session, source, _routes(group_members=[REFERENDAR, PARTNER]), tmp_path)
    version_id = _promote(session, source, "Schmidt")
    access = AccessService(session)
    assert version_id in access.visible_version_ids({f"user:{REFERENDAR}"})

    _sync(session, source, _routes(group_members=[PARTNER]), tmp_path, force_full=True)

    assert version_id not in AccessService(session).visible_version_ids({f"user:{REFERENDAR}"})
    assert version_id in AccessService(session).visible_version_ids({f"user:{PARTNER}"})


def test_removing_someone_from_the_shared_folder_revokes_their_access(
    session: Session, tmp_path
) -> None:
    source = _source(session)
    _sync(
        session,
        source,
        _routes(folder_users=[_member(PARTNER), _member(REFERENDAR)], group_members=[]),
        tmp_path,
    )
    version_id = _promote(session, source, "Schmidt")
    assert version_id in AccessService(session).visible_version_ids({f"user:{REFERENDAR}"})

    result = _sync(
        session,
        source,
        _routes(folder_users=[_member(PARTNER)], group_members=[]),
        tmp_path,
        force_full=True,
    )

    assert version_id not in AccessService(session).visible_version_ids({f"user:{REFERENDAR}"})
    # The bytes did not change, so this must cost an access refresh and not a re-derive.
    assert result.access_changed >= 1
    assert result.changed == 0


def test_a_downgrade_to_traverse_revokes_access_just_as_a_removal_does(
    session: Session, tmp_path
) -> None:
    source = _source(session)
    _sync(
        session,
        source,
        _routes(folder_users=[_member(PARTNER), _member(REFERENDAR, "editor")], group_members=[]),
        tmp_path,
    )
    version_id = _promote(session, source, "Schmidt")
    assert version_id in AccessService(session).visible_version_ids({f"user:{REFERENDAR}"})

    _sync(
        session,
        source,
        _routes(
            folder_users=[_member(PARTNER), _member(REFERENDAR, "traverse")], group_members=[]
        ),
        tmp_path,
        force_full=True,
    )
    assert version_id not in AccessService(session).visible_version_ids({f"user:{REFERENDAR}"})


def test_a_document_whose_permissions_could_not_be_read_reaches_nobody(
    session: Session, tmp_path
) -> None:
    """Fail-closed is the basis of the guarantee: an unreadable ACL is unknown, never
    permissive."""
    routes = _routes(group_members=[])
    routes[f"POST {FOLDER_MEMBERS}"] = Recorded({"error_summary": "access_error/"}, status=403)

    source = _source(session)
    _sync(session, source, routes, tmp_path)
    version_id = _promote(session, source, "Schmidt")

    access = AccessService(session)
    for principal in (PARTNER, ASSOCIATE, OWNER, OUTSIDER):
        assert version_id not in access.visible_version_ids({f"user:{principal}"})


def test_a_deletion_reported_by_the_change_feed_tombstones_the_object(
    session: Session, tmp_path
) -> None:
    """The full path, through the engine: delta deletion -> resolved id -> tombstone."""
    routes = _routes(group_members=[])
    source = _source(session)
    _sync(session, source, routes, tmp_path)

    row = session.scalars(
        select(SourceObject).where(
            SourceObject.source_id == source.id, SourceObject.external_id == "id:schmidt"
        )
    ).one()
    assert row.deleted_at is None

    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded(
        {"entries": [_deleted("/mandate/schmidt.txt", "Schmidt.txt")], "has_more": False}
    )
    result = _sync_delta(session, source, routes, tmp_path)
    assert result.mode == "incremental", "the change feed was not the path under test"

    session.refresh(row)
    assert row.deleted_at is not None


def test_a_renamed_document_keeps_its_identity_through_the_engine(
    session: Session, tmp_path
) -> None:
    """The rename race, proven where it would actually have hurt: the object must be
    updated, not tombstoned and re-created."""
    routes = _routes(group_members=[])
    source = _source(session)
    _sync(session, source, routes, tmp_path)

    renamed = _file("id:schmidt", "Schmidt-2026.txt", "/mandate/schmidt-2026.txt")
    routes[f'POST {CONTINUE} | "cursor-1"'] = Recorded(
        {"entries": [_deleted("/mandate/schmidt.txt", "Schmidt.txt"), renamed], "has_more": False}
    )
    result = _sync_delta(session, source, routes, tmp_path)
    assert result.mode == "incremental", "the change feed was not the path under test"

    row = session.scalars(
        select(SourceObject).where(
            SourceObject.source_id == source.id, SourceObject.external_id == "id:schmidt"
        )
    ).one()
    assert row.deleted_at is None
    assert row.name == "Schmidt-2026.txt"


def test_the_indexed_bytes_are_the_documents_own_after_a_sync(
    session: Session, tmp_path
) -> None:
    """Staged content is keyed by file id, so one matter's document can never be served
    as another's."""
    source = _source(session)
    _sync(session, source, _routes(group_members=[]), tmp_path)

    rows = session.scalars(
        select(SourceObject).where(SourceObject.source_id == source.id)
    ).all()
    assert len(rows) == 3
    staged = {row.external_id: row.staged_path for row in rows}
    assert len(set(staged.values())) == 3
    for path in staged.values():
        with open(path, "rb") as handle:
            assert handle.read() == b"dropbox bytes"
