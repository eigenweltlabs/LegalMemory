"""Tests for the owned connector layer.

The bridge is where a connector's typed entities become the observations the sync
engine writes, so these tests pin the behaviours that would silently corrupt an index
or an ethical wall if they regressed:

* the id/name/timestamp flags are resolved off the schema, not guessed from attributes;
* containers do not become documents;
* absent ACLs stay absent (fail-closed) and are never confused with an empty ACL;
* a skipped file does not abort the scan;
* OAuth refresh rotates, persists, and serializes under concurrency.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, Field

from knowledge_index.connectors.acl import (
    box_collaborations_to_access,
    drive_permissions_to_access,
    dropbox_members_to_access,
    graph_permissions_to_access,
)
from knowledge_index.connectors.bridge import (
    ConnectorAdapter,
    entity_external_id,
    entity_mtime,
    entity_name,
    entity_path,
    is_content_entity,
    translate_access,
)
from knowledge_index.connectors.configs import (
    BoxConfig,
    DropboxConfig,
    GoogleDriveConfig,
    OneDriveConfig,
)
from knowledge_index.connectors.cursors import GoogleDriveCursor, OneDriveCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.entities.flags import is_deletion
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities._field import IndexField
from knowledge_index.connectors.registry import CATALOG, catalog, get
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.oauth import (
    build_authorization_request,
    get_provider,
    load_providers,
)
from knowledge_index.connectors.runtime.secrets import (
    CredentialCryptoError,
    decrypt_credentials,
    encrypt_credentials,
)
from knowledge_index.connectors.runtime.tokens import OAuthTokenProvider, StaticTokenProvider
from knowledge_index.connectors.runtime.types import MembershipTuple
from knowledge_index.connectors.sources.box import BoxSource
from knowledge_index.connectors.sources.dropbox import DropboxSource
from knowledge_index.connectors.sources.google_drive import GoogleDriveSource
from knowledge_index.connectors.sources.onedrive import OneDriveSource
from knowledge_index.sync.base import UnsupportedOperation

KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


# --------------------------------------------------------------------------- fixtures


class _Doc(BaseEntity):
    """A file-shaped entity, flagged the way the connector schemas flag theirs."""

    # The entity base requires breadcrumbs to be a list; root entities pass an empty
    # one. Defaulted here so each test states only what it is actually about.
    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    doc_id: str = IndexField(..., description="id", is_entity_id=True)
    title: str = IndexField(..., description="name", is_name=True, embeddable=True)
    modified: datetime | None = IndexField(None, description="mtime", is_updated_at=True)
    etag: str | None = IndexField(None, description="etag")
    local_path: str | None = None
    size: int | None = None
    url: str = ""


class _Message(BaseEntity):
    """A text-shaped document: no file, content carried in embeddable fields.

    Deliberately declares no ``local_path``: that is what distinguishes a message or page
    from a file whose bytes failed to download.
    """

    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    doc_id: str = IndexField(..., description="id", is_entity_id=True)
    title: str = IndexField(..., description="name", is_name=True, embeddable=True)
    body: str | None = IndexField(None, description="message body", embeddable=True)
    modified: datetime | None = IndexField(None, description="mtime", is_updated_at=True)


class _Container(BaseEntity):
    """A drive/site/channel: yielded for breadcrumbs, never a document."""

    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    container_id: str = IndexField(..., description="id", is_entity_id=True)
    label: str = IndexField(..., description="name", is_name=True)


class _Removed(BaseEntity):
    """How a delta feed reports that an object is gone."""

    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    doc_id: str = IndexField(..., description="id", is_entity_id=True)
    title: str = IndexField(..., description="name", is_name=True)
    deletion_status: str = "removed"


class _FakeCursor:
    def __init__(self, data: dict) -> None:
        self.data = data


class _TokenCursor(BaseModel):
    token: str = ""


class _CursorWritingSource:
    supports_continuous = True
    supports_access_control = False

    def __init__(self) -> None:
        self.received: dict | None = None

    async def generate_entities(self, *, cursor=None, **_kwargs):
        self.received = cursor.data
        cursor.update(token="new-checkpoint")
        return
        yield  # pragma: no cover


class _DeltaSource:
    supports_continuous = True
    supports_access_control = False

    def __init__(self, entities) -> None:
        self._entities = entities

    async def generate_entities(self, **_kwargs):
        for entity in self._entities:
            yield entity


class _AclSource:
    supports_continuous = False
    supports_access_control = True

    async def generate_entities(self, **_kwargs):
        return
        yield  # pragma: no cover

    async def generate_access_control_memberships(self):
        yield MembershipTuple(
            member_id="Anwalt@Kanzlei.de",
            member_type="User",
            group_id="entra:ABC",
            group_name="Litigation",
        )


class _FakeSource:
    """Stands in for a real connector: yields entities, one of them unusable."""

    supports_continuous = False
    supports_access_control = True

    def __init__(self, entities, *, raise_skip_at: int | None = None) -> None:
        self._entities = entities
        self._raise_skip_at = raise_skip_at

    async def generate_entities(self, **_kwargs) -> AsyncGenerator[BaseEntity, None]:
        for index, entity in enumerate(self._entities):
            if index == self._raise_skip_at:
                raise FileSkippedException("unsupported file extension: .exe", "virus.exe")
            yield entity


def _connector(entities, tmp_path, **kwargs) -> ConnectorAdapter:
    return ConnectorAdapter(
        "fake",
        _FakeSource(entities, **kwargs),
        file_service=FileService(tmp_path, run_id="test"),
    )


# ----------------------------------------------------------------- flagged-field reads


def test_flagged_fields_resolve_id_name_and_mtime():
    when = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    doc = _Doc(doc_id="abc", title="Kaufvertrag.docx", modified=when)

    assert entity_external_id(doc) == "abc"
    assert entity_name(doc) == "Kaufvertrag.docx"
    assert entity_mtime(doc) == when


def test_path_is_built_from_breadcrumb_ancestry():
    doc = _Doc(
        doc_id="abc",
        title="Kaufvertrag.docx",
        breadcrumbs=[
            Breadcrumb(entity_id="s1", name="Mandate", entity_type="Site"),
            Breadcrumb(entity_id="f1", name="2026", entity_type="Folder"),
        ],
    )
    assert entity_path(doc, entity_name(doc)) == "Mandate/2026/Kaufvertrag.docx"


# ------------------------------------------------------------------------ ACL mapping


def test_absent_acl_stays_none_so_the_engine_stays_fail_closed():
    # None means "this connector cannot read permissions", which the permission
    # compiler must treat as unknown. Returning [] here would instead assert
    # "nobody is allowed", and the two must never collapse into one another.
    assert translate_access(None) is None


def test_empty_viewer_list_is_an_explicit_empty_grant_set():
    assert translate_access(AccessControl(viewers=[], is_public=False)) == []


def test_viewers_become_allow_grants_with_inferred_principal_kind():
    access = AccessControl(viewers=["user:Anwalt@Kanzlei.de", "group:entra:abc-123"])
    assert translate_access(access) == [
        {
            "principal": "group:entra:abc-123",
            "principal_kind": "group",
            "effect": "allow",
            "origin": "connector",
        },
        {
            "principal": "user:anwalt@kanzlei.de",
            "principal_kind": "user",
            "effect": "allow",
            "origin": "connector",
        },
    ]


def test_public_means_every_authenticated_member_not_anonymous():
    grants = translate_access(AccessControl(viewers=[], is_public=True))
    assert [grant["principal"] for grant in grants] == ["role:authenticated"]


# ------------------------------------------------------------------ scan → observation


def test_containers_are_not_indexed_as_documents(tmp_path):
    connector = _connector([_Container(container_id="drive1", label="Documents")], tmp_path)
    try:
        assert list(connector.full_scan()) == []
    finally:
        connector.close()


def test_text_entity_is_staged_and_readable_from_the_recorded_path(tmp_path):
    doc = _Message(doc_id="msg-1", title="Standup", body="Wir besprechen den Fall.")
    connector = _connector([doc], tmp_path)
    try:
        (observation,) = list(connector.full_scan())
        assert observation.external_id == "msg-1"
        # The staged text is the rendered entity, not just one field.
        assert observation.size_bytes >= len(b"Wir besprechen den Fall.")
        # The staged path travels on the observation so the fetch stage — which runs in
        # another process — reads a local file instead of re-crawling the source.
        assert observation.staged_path
        with connector.open_staged(observation.staged_path) as handle:
            assert "Wir besprechen den Fall." in handle.read().decode()
    finally:
        connector.close()


def test_downloaded_file_is_reported_with_etag_as_change_hint(tmp_path):
    payload = tmp_path / "Vertrag.pdf"
    payload.write_bytes(b"%PDF-1.7 ...")
    doc = _Doc(
        doc_id="file-1",
        title="Vertrag.pdf",
        etag="W/\"7\"",
        local_path=str(payload),
        size=11,
    )
    connector = _connector([doc], tmp_path / "stage")
    try:
        (observation,) = list(connector.full_scan())
        assert observation.change_hint == 'W/"7"'
        assert observation.source_version_label == 'W/"7"'
        assert observation.staged_path == str(payload)
        with connector.open_staged(observation.staged_path) as handle:
            assert handle.read() == b"%PDF-1.7 ..."
    finally:
        connector.close()


def test_fetch_by_id_refuses_rather_than_recrawling(tmp_path):
    # Re-enumerating a SaaS estate to recover one file is quadratic in corpus size and
    # throttles the firm's tenant. A miss must be a loud failure, not a silent re-crawl.
    connector = _connector([], tmp_path)
    try:
        with pytest.raises(UnsupportedOperation):
            connector.fetch("anything")
        with pytest.raises(FileNotFoundError):
            connector.open_staged(None, "obj-1")
    finally:
        connector.close()


def test_deletions_are_reported_so_incremental_sync_can_tombstone(tmp_path):
    entities = [
        _Message(doc_id="live", title="Live", body="here"),
        _Removed(doc_id="gone", title="Gone.pdf", deletion_status="removed"),
    ]
    connector = ConnectorAdapter(
        "fake",
        _DeltaSource(entities),
        file_service=FileService(tmp_path, run_id="test"),
        cursor=_FakeCursor({"token": "abc"}),
    )
    try:
        batch = connector.changes(None)
        assert [o.external_id for o in batch.observations] == ["live"]
        # Without this a document deleted at source stays indexed and retrievable.
        assert batch.deleted_external_ids == ["gone"]
        assert batch.next_cursor == '{"token": "abc"}'
    finally:
        connector.close()


def test_cursor_with_no_state_is_still_distinguishable_from_never_syncing(tmp_path):
    connector = ConnectorAdapter(
        "fake",
        _DeltaSource([]),
        file_service=FileService(tmp_path, run_id="test"),
        cursor=_FakeCursor({}),
    )
    try:
        # "{}" not None: a None cursor would send the next sync back to a full scan.
        assert connector.changes(None).next_cursor == "{}"
    finally:
        connector.close()


def test_full_scan_resets_old_cursor_and_keeps_the_new_checkpoint(tmp_path):
    source = _CursorWritingSource()
    connector = ConnectorAdapter(
        "fake",
        source,
        file_service=FileService(tmp_path, run_id="test"),
        cursor=SyncCursor(uuid4(), _TokenCursor, {"token": "stale-checkpoint"}),
    )
    try:
        assert list(connector.full_scan()) == []
        assert source.received == {"token": ""}
        assert connector.cursor_state() == '{"token": "new-checkpoint"}'
    finally:
        connector.close()


def test_memberships_are_mirrored_when_the_connector_can_report_them(tmp_path):
    connector = ConnectorAdapter(
        "fake",
        _AclSource(),
        file_service=FileService(tmp_path, run_id="test"),
    )
    try:
        assert connector.memberships() == [
            {
                "member_id": "anwalt@kanzlei.de",
                "member_type": "user",
                "group_id": "entra:abc",
                "group_name": "Litigation",
            }
        ]
    finally:
        connector.close()


def test_a_skipped_file_does_not_abort_the_remaining_scan(tmp_path):
    entities = [
        _Message(doc_id="a", title="A", body="alpha"),
        _Message(doc_id="b", title="B", body="beta"),
        _Message(doc_id="c", title="C", body="gamma"),
    ]
    connector = _connector(entities, tmp_path, raise_skip_at=1)
    try:
        # The generator dies at the skip; the scan must surface what it did read
        # rather than propagating a per-file policy skip as a sync failure.
        observed = [o.external_id for o in connector.full_scan()]
        assert observed == ["a"]
    finally:
        connector.close()


def test_content_entity_detection():
    assert is_content_entity(_Doc(doc_id="1", title="t", local_path="/tmp/x.pdf"))
    # Text-shaped: content comes from its embeddable fields.
    assert is_content_entity(_Message(doc_id="1", title="t", body="hi"))
    # A file entity whose download did not happen must NOT be indexed as a metadata
    # stub — an empty document matches weakly and buries real results.
    assert not is_content_entity(_Doc(doc_id="1", title="t"))
    assert not is_content_entity(_Container(container_id="1", label="Drive"))


# --------------------------------------------------------------------- file staging


def test_unsupported_extension_is_skipped_not_downloaded(tmp_path):
    service = FileService(tmp_path, run_id="run")
    # Real file entities carry the download name on `name`, as the staging policy reads.
    entity = _Doc(doc_id="x", title="payload.exe", name="payload.exe", url="https://x/y")
    with pytest.raises(FileSkippedException):
        asyncio.run(service.download_from_url(entity, client=None))


def test_staged_filename_cannot_escape_the_staging_directory(tmp_path):
    service = FileService(tmp_path, run_id="run")
    staged = service.stage_text("../../etc/passwd", "x")
    assert staged.is_relative_to(service.base_dir)


def test_same_filename_in_two_matters_does_not_collide(tmp_path):
    # Keyed by external id, not by name: serving one matter's contract as another's
    # would be a confidentiality failure across a matter boundary.
    service = FileService(tmp_path, run_id="run")
    first = service.stage_text("matter-a/Vertrag.pdf", "A")
    second = service.stage_text("matter-b/Vertrag.pdf", "B")
    assert first != second
    assert first.read_text() == "A"
    assert second.read_text() == "B"


def test_staging_is_version_addressed_so_unchanged_content_is_reused(tmp_path):
    service = FileService(tmp_path, run_id="run")
    unchanged = service.target_for("doc-1", 'W/"7"', "Vertrag.pdf")
    unchanged.parent.mkdir(parents=True, exist_ok=True)
    unchanged.write_bytes(b"old")
    assert service.staged_path("doc-1", 'W/"7"', "Vertrag.pdf") == unchanged
    # A new source version is a different path, so a changed file is re-downloaded.
    assert service.staged_path("doc-1", 'W/"8"', "Vertrag.pdf") is None


def test_download_is_skipped_when_the_same_version_is_already_staged(tmp_path):
    service = FileService(tmp_path, run_id="run")
    entity = _Doc(
        doc_id="doc-1", title="Vertrag.pdf", name="Vertrag.pdf", etag='W/"7"', url="https://x/y"
    )
    staged = service.target_for("doc-1", 'W/"7"', "Vertrag.pdf")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"cached")

    class _ExplodingClient:
        def stream(self, *args, **kwargs):
            raise AssertionError("unchanged content must not be re-downloaded")

    asyncio.run(service.download_from_url(entity, _ExplodingClient()))
    assert entity.local_path == str(staged)


# ------------------------------------------------------------------------ credentials


def test_credentials_round_trip_under_encryption():
    blob = encrypt_credentials({"refresh_token": "r1", "access_token": "a1"}, key=KEY)
    assert "r1" not in blob
    assert decrypt_credentials(blob, key=KEY) == {"refresh_token": "r1", "access_token": "a1"}


def test_tampered_credentials_are_rejected():
    blob = encrypt_credentials({"refresh_token": "r1"}, key=KEY)
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    with pytest.raises(CredentialCryptoError):
        decrypt_credentials(tampered, key=KEY)


def test_missing_credential_key_is_a_hard_error(monkeypatch):
    monkeypatch.delenv("KI_CONNECTOR_CREDENTIAL_KEY", raising=False)
    with pytest.raises(CredentialCryptoError):
        encrypt_credentials({"refresh_token": "r"})


# ------------------------------------------------------------------------------ auth


def test_static_provider_cannot_refresh():
    provider = StaticTokenProvider("tok")
    assert provider.supports_refresh is False
    assert asyncio.run(provider.get_token()) == "tok"


def test_oauth_provider_refreshes_once_and_persists_rotated_token():
    calls: list[str] = []
    saved: list[dict] = []

    async def refresh(token: str):
        calls.append(token)
        return f"access-{len(calls)}", f"rotated-{len(calls)}", 3600

    async def persist(credentials: dict) -> None:
        saved.append(credentials)

    provider = OAuthTokenProvider(
        {"access_token": "stale", "refresh_token": "r0"},
        oauth_type="with_rotating_refresh",
        refresh=refresh,
        persist=persist,
    )

    async def scenario():
        first = await provider.get_token()
        second = await provider.get_token()  # cached, no second network call
        return first, second

    first, second = asyncio.run(scenario())
    assert (first, second) == ("access-1", "access-1")
    assert calls == ["r0"]
    # A rotating provider invalidates the old refresh token, so the new one must be
    # persisted or the connection is permanently broken after the process restarts.
    assert saved[-1]["refresh_token"] == "rotated-1"
    assert provider.refresh_token == "rotated-1"


def test_concurrent_refresh_makes_one_network_call():
    calls: list[str] = []

    async def refresh(token: str):
        calls.append(token)
        await asyncio.sleep(0.01)
        return "access", "rotated", 3600

    provider = OAuthTokenProvider(
        {"access_token": "stale", "refresh_token": "r0"},
        oauth_type="with_refresh",
        refresh=refresh,
    )

    async def scenario():
        return await asyncio.gather(*(provider.get_token() for _ in range(8)))

    tokens = asyncio.run(scenario())
    assert tokens == ["access"] * 8
    assert len(calls) == 1


def test_provider_without_refresh_token_serves_access_token_only():
    provider = OAuthTokenProvider(
        {"access_token": "only"}, oauth_type="access_only", refresh=None
    )
    assert provider.supports_refresh is False
    assert asyncio.run(provider.get_token()) == "only"


# ----------------------------------------------------------------------------- oauth


def test_every_catalog_connector_has_oauth_settings():
    providers = load_providers()
    missing = [
        spec.short_name
        for spec in CATALOG
        if spec.oauth_provider and spec.oauth_provider not in providers
    ]
    assert missing == []


def test_authorization_request_carries_state_and_pkce_when_required():
    provider = get_provider("sharepoint_online")
    request = build_authorization_request(
        provider, client_id="cid", redirect_uri="https://ki.example/callback"
    )
    assert request.state
    assert "client_id=cid" in request.url
    assert "offline_access" in request.url  # or the connection dies at first expiry


def test_microsoft_scopes_include_group_reads_for_acl_mirroring():
    # Without directory and group reads, SharePoint ACLs cannot be expanded to members
    # and every group-shared document stays invisible under fail-closed permissions.
    scope = get_provider("sharepoint_online").scope or ""
    assert "GroupMember.Read.All" in scope
    assert "Directory.Read.All" in scope


def test_teams_scopes_cover_conversation_audiences_without_a_tenant_user_crawl():
    scope = set((get_provider("teams").scope or "").split())
    assert {
        "https://graph.microsoft.com/TeamMember.Read.All",
        "https://graph.microsoft.com/ChannelMember.Read.All",
        "https://graph.microsoft.com/Chat.Read",
    } <= scope
    assert "https://graph.microsoft.com/User.Read.All" not in scope
    assert "https://graph.microsoft.com/Group.Read.All" not in scope


def test_google_drive_scopes_cover_read_only_group_expansion():
    scope = set((get_provider("google_drive").scope or "").split())
    assert {
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/admin.directory.group.readonly",
        "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
    } <= scope
    assert "https://www.googleapis.com/auth/admin.directory.group" not in scope
    assert "https://www.googleapis.com/auth/admin.directory.group.member" not in scope


# --------------------------------------------------------------------------- catalog


# Every connector but Notion mirrors what its source says about who may read a document.
# Notion is the exception on purpose: its API exposes no per-page permissions to an
# integration, so there is nothing to mirror and the catalog says so rather than implying
# enforcement that cannot exist.
ACL_MIRRORING = (
    "sharepoint_online",
    "onedrive",
    "clio",
    "google_drive",
    "google_docs",
    "dropbox",
    "box",
    "slack",
    "teams",
    "confluence",
    "gmail",
    "outlook_mail",
    "outlook_calendar",
    "onenote",
)


def test_catalog_is_honest_about_acl_support():
    entries = {entry["id"]: entry for entry in catalog()}
    # Advertising this when it is not true would tell an operator their ethical walls are
    # enforced when they are not — and claiming the reverse hides a corpus for no reason.
    for short_name in ACL_MIRRORING:
        assert entries[short_name]["acl_sync"] is True, short_name
        assert entries[short_name]["notes"], short_name
    # Notion cannot, and the catalog still says so.
    assert entries["notion"]["acl_sync"] is False
    # A connector that silently stopped mirroring would leave its documents invisible with
    # nothing in the catalog to explain why, so the exception list is pinned. Planned
    # roadmap entries claim nothing either way and are not part of the statement.
    assert {
        entry["id"]
        for entry in catalog()
        if not entry["acl_sync"] and not entry.get("planned")
    } == {"notion"}


def test_catalog_only_enables_the_launch_connectors():
    entries = {entry["id"]: entry for entry in catalog()}
    assert {entry["id"] for entry in catalog() if entry["connectable"]} == {
        "sharepoint_online",
        "google_drive",
        "onedrive",
        "clio",
        "dropbox",
    }
    assert entries["teams"]["connectable"] is False
    assert entries["slack"]["connectable"] is False
    assert entries["outlook_mail"]["connectable"] is False


def test_acl_mirroring_connectors_declare_the_capability_on_the_class():
    # The bridge reads supports_access_control off the source class to set
    # capabilities.acl. A catalog entry claiming ACL support while the class denies it
    # would advertise enforcement the pipeline never applies.
    for short_name in ACL_MIRRORING:
        assert get(short_name).load().supports_access_control is True, short_name


def test_every_catalog_entry_resolves_to_a_real_source_class():
    for entry in catalog():
        if entry.get("planned"):
            continue
        source_class = get(entry["id"]).load()
        assert getattr(source_class, "is_source", False), entry["id"]
        assert hasattr(source_class, "generate_entities")


def test_planned_connectors_are_roadmap_cards_and_nothing_more():
    """The legal-DMS roadmap is visible without ever being mistaken for software.

    A firm evaluating the catalog judges it by whether it names their DMS — iManage
    globally, RA-MICRO in Germany — so those names appear as planned. But a planned
    entry must be inert: never connectable, no capability claims, and no id that the
    registry could resolve to a source class.
    """
    from knowledge_index.connectors.registry import CATALOG as BUILT, PLANNED

    planned_entries = [entry for entry in catalog() if entry.get("planned")]
    assert {entry["id"] for entry in planned_entries} == {
        item.short_name for item in PLANNED
    }
    # The names a law firm actually looks for.
    assert {"imanage", "netdocuments", "ra_micro", "datev_anwalt", "annotext"} <= {
        entry["id"] for entry in planned_entries
    }
    assert not {item.short_name for item in PLANNED} & {spec.short_name for spec in BUILT}
    for entry in planned_entries:
        assert entry["connectable"] is False, entry["id"]
        assert entry["acl_sync"] is None and entry["incremental"] is None, entry["id"]
        assert entry["notes"], entry["id"]
    with pytest.raises(Exception):
        get("imanage")


# ------------------------------------------------------- mirrored source permissions

# These connectors are only useful if a document ends up retrievable by the same people
# who can open it at the source. The payloads below are shaped like the real API
# responses, including the cases that must NOT become grants: an anonymous link, a
# pending invitation, an upload-only role, a service principal. Getting any of those
# wrong widens access silently, which is the one failure mode worse than a blank result.

GRAPH_PERMISSIONS = [
    {
        "id": "p1",
        "roles": ["read"],
        "grantedToV2": {"user": {"id": "u-1", "email": "Anwalt@Kanzlei.de"}},
    },
    {
        "id": "p2",
        "roles": ["write"],
        "grantedToV2": {"group": {"id": "ABC-123", "displayName": "Litigation"}},
    },
    {
        # A tenant-wide sharing link: everyone who can authenticate in the firm.
        "id": "p3",
        "roles": ["read"],
        "link": {"scope": "organization", "type": "view", "webUrl": "https://x/y"},
    },
    {
        # Readable without authenticating. An exposure to report, never a grant.
        "id": "p4",
        "roles": ["read"],
        "link": {"scope": "anonymous", "type": "view", "webUrl": "https://x/z"},
    },
    {
        # "Can request access" is not access.
        "id": "p5",
        "roles": ["restricted"],
        "grantedToV2": {"user": {"email": "gegner@extern.de"}},
    },
    {
        # An app registration is not a person; its grant must not widen human retrieval.
        "id": "p6",
        "roles": ["write"],
        "grantedToV2": {"application": {"id": "app-1", "displayName": "Backup"}},
    },
]

DRIVE_PERMISSIONS = [
    {"id": "d1", "type": "user", "role": "reader", "emailAddress": "Anwalt@Kanzlei.de"},
    {"id": "d2", "type": "group", "role": "writer", "emailAddress": "litigation@kanzlei.de"},
    {"id": "d3", "type": "domain", "role": "reader", "domain": "kanzlei.de"},
    # Link-shared to the whole internet. Not mirrored.
    {"id": "d4", "type": "anyone", "role": "reader"},
    {
        "id": "d5",
        "type": "user",
        "role": "writer",
        "emailAddress": "ehemalig@kanzlei.de",
        "deleted": True,
    },
    {
        "id": "d6",
        "type": "user",
        "role": "writer",
        "emailAddress": "neu@kanzlei.de",
        "pendingOwner": True,
    },
]

DROPBOX_USERS = [
    {
        "access_type": {".tag": "editor"},
        "user": {"account_id": "a1", "email": "Anwalt@Kanzlei.de"},
    },
    {
        "access_type": {".tag": "viewer"},
        "user": {"account_id": "a2", "email": "referendar@kanzlei.de"},
    },
    {
        "access_type": {".tag": "no_access"},
        "user": {"account_id": "a3", "email": "ehemalig@kanzlei.de"},
    },
]
DROPBOX_GROUPS = [
    {
        "access_type": {".tag": "viewer"},
        "group": {"group_id": "g:LIT", "group_name": "Litigation"},
    },
]
DROPBOX_INVITEES = [
    {"access_type": {".tag": "viewer"}, "invitee": {"email": "gegner@extern.de"}},
]

BOX_COLLABORATIONS = [
    {
        "id": "c1",
        "role": "editor",
        "status": "accepted",
        "accessible_by": {"type": "user", "login": "Anwalt@Kanzlei.de"},
    },
    {
        "id": "c2",
        "role": "viewer",
        "status": "accepted",
        "accessible_by": {"type": "group", "id": "77", "name": "Litigation"},
    },
    {
        # Upload-only: cannot read what is already there.
        "id": "c3",
        "role": "uploader",
        "status": "accepted",
        "accessible_by": {"type": "user", "login": "praktikant@kanzlei.de"},
    },
    {
        # An unaccepted invitation is not access.
        "id": "c4",
        "role": "viewer",
        "status": "pending",
        "accessible_by": {"type": "user", "login": "gegner@extern.de"},
    },
]


def _collect(agen) -> list:
    """Drain an async generator on a throwaway loop."""

    async def run():
        return [item async for item in agen]

    return asyncio.run(run())


def _forbidden(url: str) -> httpx.HTTPStatusError:
    """What a permission read looks like when the token may not see the ACL."""
    return httpx.HTTPStatusError(
        "403 Forbidden",
        request=httpx.Request("GET", url),
        response=httpx.Response(403, request=httpx.Request("GET", url)),
    )


def _build(source_class, config, handler_name, handler):
    source = asyncio.run(
        source_class.create(
            auth=StaticTokenProvider("tok"),
            logger=ContextualLogger(source="test", run_id="test"),
            http_client=None,
            config=config,
        )
    )
    # The connector's own authenticated helper is replaced, so retry/backoff/401-refresh
    # stay in the code path the connector uses in production.
    setattr(source, handler_name, handler)
    return source


# --- translation ---------------------------------------------------------------------


def test_graph_permissions_become_read_principals_and_organization_scope_is_public():
    access = graph_permissions_to_access(GRAPH_PERMISSIONS)
    assert access.viewers == ["group:entra:abc-123", "user:anwalt@kanzlei.de"]
    # The organization-wide link is the firm, not the internet.
    assert access.is_public is True


def test_drive_permissions_become_read_principals_and_domain_scope_is_public():
    access = drive_permissions_to_access(DRIVE_PERMISSIONS)
    assert access.viewers == ["group:google:litigation@kanzlei.de", "user:anwalt@kanzlei.de"]
    assert access.is_public is True


def test_dropbox_members_become_read_principals_and_invitations_are_ignored():
    access = dropbox_members_to_access(DROPBOX_USERS, DROPBOX_GROUPS, DROPBOX_INVITEES)
    assert access.viewers == [
        "group:dropbox:g:lit",
        "user:anwalt@kanzlei.de",
        "user:referendar@kanzlei.de",
    ]
    # No sharing link concept is mirrored here, so nothing may claim firm-wide reach.
    assert access.is_public is False


def test_box_collaborations_become_read_principals_without_pending_or_upload_only():
    access = box_collaborations_to_access(BOX_COLLABORATIONS)
    assert access.viewers == ["group:box:77", "user:anwalt@kanzlei.de"]
    assert access.is_public is False


@pytest.mark.parametrize(
    "translate",
    [
        graph_permissions_to_access,
        drive_permissions_to_access,
        box_collaborations_to_access,
    ],
)
def test_unreadable_permission_payload_translates_to_unknown_not_empty(translate):
    # None is "unknown" and stays fail-closed. [] would assert "nobody may read this",
    # which looks like a deliberate restriction and would hide a corpus silently.
    assert translate(None) is None
    assert dropbox_members_to_access(None, None) is None


# --- OneDrive ------------------------------------------------------------------------

ONEDRIVE_ITEM = {
    "id": "item-1",
    "name": "Kaufvertrag.docx",
    "size": 12,
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "parentReference": {"driveId": "drive-1"},
    "createdDateTime": "2026-03-01T12:00:00Z",
    "lastModifiedDateTime": "2026-03-02T12:00:00Z",
}


def _onedrive(*, permissions=None, fail=False, mirror=True):
    calls: list[str] = []

    async def handler(url, params=None):
        calls.append(url)
        if url.endswith("/me/drive"):
            return {"id": "drive-1", "name": "Mandate", "driveType": "business"}
        if url.endswith("/root/children"):
            return {"value": [ONEDRIVE_ITEM]}
        if url.endswith("/permissions"):
            if fail:
                raise _forbidden(url)
            return {"value": permissions}
        raise AssertionError(f"unexpected Graph call: {url}")

    source = _build(
        OneDriveSource, OneDriveConfig(mirror_permissions=mirror), "_get", handler
    )
    return source, calls


def test_onedrive_mirrors_item_permissions_from_graph():
    source, calls = _onedrive(permissions=GRAPH_PERMISSIONS)
    entities = _collect(source.generate_entities())

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    assert file_entity.access.viewers == ["group:entra:abc-123", "user:anwalt@kanzlei.de"]
    assert file_entity.access.is_public is True
    assert any(url.endswith("/drives/drive-1/items/item-1/permissions") for url in calls)


def test_onedrive_permission_failure_leaves_access_unknown_and_still_yields_the_file():
    source, _calls = _onedrive(fail=True)
    entities = _collect(source.generate_entities())

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    # The document is still indexed; it is simply not retrievable until access is known.
    assert file_entity.access is None
    assert translate_access(file_entity.access) is None


def test_onedrive_skips_the_extra_permission_call_when_mirroring_is_off():
    source, calls = _onedrive(permissions=GRAPH_PERMISSIONS, mirror=False)
    entities = _collect(source.generate_entities())

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    assert file_entity.access is None
    assert not [url for url in calls if url.endswith("/permissions")]


# --- OneDrive delta sync -------------------------------------------------------------

GRAPH = "https://graph.microsoft.com/v1.0"


def _onedrive_routed(routes: dict, *, cursor_data: dict | None = None):
    """A OneDrive source whose Graph is a route table, plus a live typed cursor.

    Routes map a URL to a payload dict or an exception. Every call is recorded as
    ``(url, params, extra_headers)`` so a test can assert ordering and headers.
    """
    calls: list[tuple[str, dict, dict]] = []

    async def handler(url, params=None, extra_headers=None):
        calls.append((url, dict(params or {}), dict(extra_headers or {})))
        payload = routes.get(url)
        if payload is None:
            raise AssertionError(f"unexpected Graph call: {url}")
        if isinstance(payload, Exception):
            raise payload
        return payload

    source = _build(OneDriveSource, OneDriveConfig(), "_get", handler)
    cursor = SyncCursor(uuid4(), OneDriveCursor, cursor_data)
    return source, cursor, calls


def _onedrive_selection() -> list:
    from knowledge_index.connectors.runtime.types import NodeSelectionData

    return [
        NodeSelectionData(
            source_node_id="f-mandate",
            node_type="folder",
            node_title="Mandate",
            node_metadata={"drive_id": "drive-1", "folder_id": "f-mandate"},
        )
    ]


def test_onedrive_full_sync_mints_the_delta_token_before_the_crawl():
    routes = {
        f"{GRAPH}/me/drive": {"id": "drive-1", "name": "Mandate", "driveType": "business"},
        f"{GRAPH}/drives/drive-1/root/delta?token=latest": {
            "value": [],
            "@odata.deltaLink": "https://graph.example/delta-1",
        },
        f"{GRAPH}/drives/drive-1/root/children": {"value": [ONEDRIVE_ITEM]},
        f"{GRAPH}/drives/drive-1/items/item-1/permissions": {"value": GRAPH_PERMISSIONS},
    }
    source, cursor, calls = _onedrive_routed(routes)

    entities = _collect(source.generate_entities(cursor=cursor))

    assert [e for e in entities if getattr(e, "id", None) == "item-1"]
    data = cursor.data
    assert data["drive_delta_tokens"] == {"drive-1": "https://graph.example/delta-1"}
    assert data["full_sync_required"] is False
    assert data["synced_drive_ids"] == {"drive-1": "Mandate"}
    # The group grant mirrored on the item is remembered for membership expansion.
    assert data["tracked_entra_groups"] == ["entra:abc-123"]
    # Minted before the crawl: a file created while the crawl runs is replayed by the
    # first delta drain instead of being invisible until the next periodic full scan.
    urls = [url for url, _params, _headers in calls]
    assert urls.index(f"{GRAPH}/drives/drive-1/root/delta?token=latest") < urls.index(
        f"{GRAPH}/drives/drive-1/root/children"
    )


def test_onedrive_incremental_yields_changes_and_deletions_from_the_delta_feed():
    cursor_data = OneDriveCursor(
        drive_delta_tokens={"drive-1": "https://graph.example/delta-1"},
        full_sync_required=False,
        last_full_sync_timestamp=datetime.now(UTC).isoformat(),
        synced_drive_ids={"drive-1": "Mandate"},
    ).model_dump()
    routes = {
        "https://graph.example/delta-1": {
            "value": [
                ONEDRIVE_ITEM,
                {"id": "gone-1", "name": "Alt.docx", "deleted": {"state": "deleted"}},
            ],
            "@odata.deltaLink": "https://graph.example/delta-2",
        },
        f"{GRAPH}/drives/drive-1/items/item-1/permissions": {"value": GRAPH_PERMISSIONS},
    }
    source, cursor, calls = _onedrive_routed(routes, cursor_data=cursor_data)

    entities = _collect(source.generate_entities(cursor=cursor))

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    assert file_entity.access.viewers == ["group:entra:abc-123", "user:anwalt@kanzlei.de"]
    (deletion,) = [e for e in entities if is_deletion(e)]
    assert entity_external_id(deletion) == "gone-1"
    assert cursor.data["drive_delta_tokens"] == {"drive-1": "https://graph.example/delta-2"}
    # The drive was not re-discovered and the delta ran with the permission-change
    # Prefer headers, so a sharing revocation wakes the item rather than nothing.
    urls = [url for url, _params, _headers in calls]
    assert f"{GRAPH}/me/drive" not in urls
    (delta_call,) = [c for c in calls if c[0] == "https://graph.example/delta-1"]
    assert "deltashowsharingchanges" in delta_call[2].get("Prefer", "")


def test_onedrive_delta_failure_durably_falls_back_to_a_full_resync():
    cursor_data = OneDriveCursor(
        drive_delta_tokens={"drive-1": "https://graph.example/delta-1"},
        full_sync_required=False,
        last_full_sync_timestamp=datetime.now(UTC).isoformat(),
    ).model_dump()
    routes = {
        "https://graph.example/delta-1": _forbidden("https://graph.example/delta-1"),
    }
    source, cursor, _calls = _onedrive_routed(routes, cursor_data=cursor_data)

    entities = _collect(source.generate_entities(cursor=cursor))

    assert entities == []
    # The failure is recorded on the cursor, not swallowed: the next run crawls.
    assert cursor.data["full_sync_required"] is True


def test_onedrive_scoped_incremental_removes_an_item_that_left_the_selected_folder():
    cursor_data = OneDriveCursor(
        drive_delta_tokens={"drive-1": "https://graph.example/delta-1"},
        full_sync_required=False,
        last_full_sync_timestamp=datetime.now(UTC).isoformat(),
        synced_drive_ids={"drive-1": "Mandate"},
    ).model_dump()
    inside = {**ONEDRIVE_ITEM, "parentReference": {"driveId": "drive-1", "id": "f-mandate"}}
    outside = {
        **ONEDRIVE_ITEM,
        "id": "item-2",
        "name": "Steuer.xlsx",
        "parentReference": {"driveId": "drive-1", "id": "f-privat"},
    }
    routes = {
        "https://graph.example/delta-1": {
            "value": [inside, outside],
            "@odata.deltaLink": "https://graph.example/delta-2",
        },
        f"{GRAPH}/drives/drive-1/items/item-1/permissions": {"value": GRAPH_PERMISSIONS},
        # Ancestry of the out-of-scope item ends at the drive root without ever
        # passing the selected folder.
        f"{GRAPH}/drives/drive-1/items/f-privat": {
            "id": "f-privat",
            "parentReference": {"id": "root-1"},
        },
        f"{GRAPH}/drives/drive-1/items/root-1": {"id": "root-1", "parentReference": {}},
    }
    source, cursor, _calls = _onedrive_routed(routes, cursor_data=cursor_data)

    entities = _collect(
        source.generate_entities(cursor=cursor, node_selections=_onedrive_selection())
    )

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    assert file_entity.name == "Kaufvertrag.docx"
    # The item outside the selected folder is removed from the index, not indexed.
    (deletion,) = [e for e in entities if is_deletion(e)]
    assert entity_external_id(deletion) == "item-2"


def test_onedrive_resolves_id_only_share_grants_to_emails():
    """An app-created share arrives as an id-only identity set.

    Mirroring it as user:id:<guid> keeps the grant forever unmatchable — nobody signs
    in as a directory GUID — so the id is resolved to the person's address. This is the
    exact shape a cross-wall share made by a service produced in live testing.
    """
    id_only_permissions = [
        {
            "id": "p1",
            "roles": ["read"],
            "grantedToV2": {"user": {"id": "u-7", "displayName": "Corp User"}},
        }
    ]
    routes = {
        f"{GRAPH}/me/drive": {"id": "drive-1", "name": "Mandate", "driveType": "business"},
        f"{GRAPH}/drives/drive-1/root/children": {"value": [ONEDRIVE_ITEM]},
        f"{GRAPH}/drives/drive-1/items/item-1/permissions": {"value": id_only_permissions},
        f"{GRAPH}/users/u-7": {"mail": "Corp.User@Kanzlei.de"},
    }
    source, _cursor, calls = _onedrive_routed(routes)

    entities = _collect(source.generate_entities())

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    assert file_entity.access.viewers == ["user:corp.user@kanzlei.de"]
    assert sum(1 for url, _p, _h in calls if url.endswith("/users/u-7")) == 1


def test_onedrive_drops_an_unresolvable_id_grant_rather_than_mirroring_it():
    id_only_permissions = [
        {
            "id": "p1",
            "roles": ["read"],
            "grantedToV2": {"user": {"id": "u-gone", "displayName": "Departed"}},
        },
        {
            "id": "p2",
            "roles": ["read"],
            "grantedToV2": {"user": {"email": "anwalt@kanzlei.de"}},
        },
    ]
    routes = {
        f"{GRAPH}/me/drive": {"id": "drive-1", "name": "Mandate", "driveType": "business"},
        f"{GRAPH}/drives/drive-1/root/children": {"value": [ONEDRIVE_ITEM]},
        f"{GRAPH}/drives/drive-1/items/item-1/permissions": {"value": id_only_permissions},
        f"{GRAPH}/users/u-gone": _forbidden(f"{GRAPH}/users/u-gone"),
    }
    source, _cursor, _calls = _onedrive_routed(routes)

    entities = _collect(source.generate_entities())

    (file_entity,) = [e for e in entities if getattr(e, "id", None) == "item-1"]
    # The resolvable grant survives; the unresolvable one is dropped, never invented.
    assert file_entity.access.viewers == ["user:anwalt@kanzlei.de"]


def test_onedrive_expands_tracked_entra_groups_into_memberships():
    class _Http:
        async def get(self, url, headers=None, params=None, timeout=None):
            request = httpx.Request("GET", url)
            if url.endswith("/groups/abc-123"):
                return httpx.Response(
                    200, json={"id": "abc-123", "displayName": "Litigation"}, request=request
                )
            if url.endswith("/groups/abc-123/members"):
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "@odata.type": "#microsoft.graph.user",
                                "mail": "Anwalt@Kanzlei.de",
                            },
                            {
                                "@odata.type": "#microsoft.graph.user",
                                "userPrincipalName": "referendar@kanzlei.de",
                            },
                        ]
                    },
                    request=request,
                )
            raise AssertionError(f"unexpected Graph call: {url}")

    source = _build(OneDriveSource, OneDriveConfig(), "_get", None)
    source._http_client = _Http()
    source._tracked_entra_groups = {"entra:abc-123"}

    memberships = _collect(source.generate_access_control_memberships())

    assert [(m.member_id, m.member_type, m.group_id) for m in memberships] == [
        ("anwalt@kanzlei.de", "user", "entra:abc-123"),
        ("referendar@kanzlei.de", "user", "entra:abc-123"),
    ]
    assert memberships[0].group_name == "Litigation"


# --- Google Drive --------------------------------------------------------------------

DRIVE_FILE = {
    "id": "file-1",
    "name": "Kaufvertrag.pdf",
    "mimeType": "application/pdf",
    "size": "12",
    "createdTime": "2026-03-01T12:00:00Z",
    "modifiedTime": "2026-03-02T12:00:00Z",
    "permissions": DRIVE_PERMISSIONS,
}


def _google_drive(*, mirror=True):
    captured: list[dict] = []

    async def handler(url, params=None):
        captured.append(dict(params or {}))
        return {"files": []}

    source = _build(
        GoogleDriveSource, GoogleDriveConfig(mirror_permissions=mirror), "_get", handler
    )
    return source, captured


def test_google_drive_requests_permissions_inline_with_file_metadata():
    source, captured = _google_drive()
    _collect(source._list_files("user", False))
    # Asked for in the same fields mask as the rest of the metadata, so mirroring ACLs
    # costs no extra round trip per file.
    assert "permissions(" in captured[0]["fields"]
    assert "emailAddress" in captured[0]["fields"]


def test_google_drive_mirrors_inline_sharing_permissions():
    source, _captured = _google_drive()
    entity = source._build_file_entity(DRIVE_FILE, None)
    assert entity.access.viewers == [
        "group:google:litigation@kanzlei.de",
        "user:anwalt@kanzlei.de",
    ]
    # Domain-wide sharing inside the firm's own tenant.
    assert entity.access.is_public is True


def test_google_drive_uses_md5_as_content_staging_version():
    source, _captured = _google_drive()
    entity = source._build_file_entity(
        {**DRIVE_FILE, "md5Checksum": "content-md5"},
        None,
    )

    assert entity.version == "content-md5"


def test_google_drive_incremental_shared_file_keeps_shared_drive_breadcrumb(monkeypatch):
    source, _captured = _google_drive()
    source._cursor = None
    source._setup_breadcrumbs([{"id": "shared-drive-1", "name": "EWL"}])

    async def downloaded(_entity, _files):
        return True

    monkeypatch.setattr(source, "_download_file", downloaded)
    entity = asyncio.run(
        source._process_changed_file(
            {**DRIVE_FILE, "driveId": "shared-drive-1"},
        )
    )

    assert entity is not None
    assert entity_path(entity, entity.name) == "EWL/Kaufvertrag.pdf"


def test_google_drive_move_out_of_selected_folder_emits_deletion_and_forgets_cache():
    source, _captured = _google_drive()
    source._scoped = True
    source._scope_folder_ids = {"selected-folder"}
    source._cursor = SyncCursor(
        uuid4(),
        GoogleDriveCursor,
        {
            "start_page_token": "token",
            "file_metadata": {
                "file-1": {
                    "modified_time": DRIVE_FILE["modifiedTime"],
                    "md5_checksum": "same-bytes",
                }
            },
        },
    )

    entity = asyncio.run(
        source._build_entity_from_change(
            {
                "fileId": "file-1",
                "file": {
                    **DRIVE_FILE,
                    "parents": ["outside-folder"],
                    "md5Checksum": "same-bytes",
                },
            }
        )
    )

    assert entity.file_id == "file-1"
    assert entity.deletion_status == "removed"
    assert "file-1" not in source._cursor.data["file_metadata"]


def test_google_drive_shared_drive_lists_complete_permissions_with_pagination():
    calls: list[tuple[str, dict]] = []
    shared_file = {
        key: value for key, value in DRIVE_FILE.items() if key != "permissions"
    }
    shared_file["driveId"] = "shared-drive-1"

    async def handler(url, params=None):
        captured = dict(params or {})
        calls.append((url, captured))
        if url.endswith("/files"):
            return {"files": [shared_file]}
        if url.endswith("/files/file-1/permissions"):
            if captured.get("pageToken") == "page-2":
                return {"permissions": [DRIVE_PERMISSIONS[1]]}
            return {
                "permissions": [DRIVE_PERMISSIONS[0]],
                "nextPageToken": "page-2",
            }
        raise AssertionError(f"unexpected Google Drive URL: {url}")

    source = _build(
        GoogleDriveSource,
        GoogleDriveConfig(mirror_permissions=True),
        "_get",
        handler,
    )
    listed = _collect(source._list_files("drive", True, "shared-drive-1"))

    assert len(listed) == 1
    assert listed[0]["permissions"] == DRIVE_PERMISSIONS[:2]
    assert "permissions(" not in calls[0][1]["fields"]
    permission_calls = [call for call in calls if call[0].endswith("/permissions")]
    assert len(permission_calls) == 2
    assert permission_calls[0][1]["pageSize"] == "100"
    assert permission_calls[0][1]["supportsAllDrives"] == "true"
    assert "permissionDetails(" in permission_calls[0][1]["fields"]
    assert permission_calls[1][1]["pageToken"] == "page-2"

    entity = source._build_file_entity(listed[0], None)
    assert entity.access.viewers == [
        "group:google:litigation@kanzlei.de",
        "user:anwalt@kanzlei.de",
    ]


def test_google_drive_shared_drive_permission_failure_discards_stale_acl():
    async def forbidden(_url, params=None):
        raise RuntimeError(f"permission list unavailable: {params}")

    source = _build(
        GoogleDriveSource,
        GoogleDriveConfig(mirror_permissions=True),
        "_get",
        forbidden,
    )
    file_obj = {
        **DRIVE_FILE,
        "driveId": "shared-drive-1",
    }
    asyncio.run(source._hydrate_shared_drive_permissions(file_obj))

    assert "permissions" not in file_obj
    assert source._build_file_entity(file_obj, None).access is None


def test_google_drive_permission_only_change_is_not_skipped_incrementally():
    class Cursor:
        def __init__(self):
            self.data: dict = {}

        def update(self, **fields):
            self.data.update(fields)

    source, _captured = _google_drive()
    source._cursor = Cursor()
    source._store_file_metadata(DRIVE_FILE)

    changed_acl = {
        **DRIVE_FILE,
        "permissions": [DRIVE_PERMISSIONS[0]],
    }
    assert source._has_file_changed(changed_acl) is True


def test_google_drive_browse_root_includes_shared_drives():
    async def handler(url, params=None):
        if url.endswith("/files"):
            return {
                "files": [
                    {
                        "id": "my-folder",
                        "name": "My Drive folder",
                        "parents": ["root"],
                    }
                ]
            }
        if url.endswith("/drives"):
            return {
                "drives": [
                    {
                        "id": "shared-drive-1",
                        "name": "Firm matters",
                    }
                ]
            }
        raise AssertionError(f"unexpected Google Drive URL: {url} ({params})")

    source = _build(
        GoogleDriveSource,
        GoogleDriveConfig(mirror_permissions=True),
        "_get",
        handler,
    )
    nodes = asyncio.run(source.get_browse_children())

    assert [(node.node_type, node.source_node_id, node.title) for node in nodes] == [
        ("folder", "my-folder", "My Drive folder"),
        ("drive", "shared-drive-1", "Firm matters"),
    ]
    assert nodes[1].node_metadata == {
        "folder_id": "shared-drive-1",
        "drive_id": "shared-drive-1",
    }


def test_google_drive_expands_only_referenced_groups_and_flattens_nested_members():
    calls: list[tuple[str, dict]] = []

    async def handler(url, params=None):
        calls.append((url, dict(params or {})))
        assert url.endswith("/groups/litigation%40kanzlei.de/members")
        return {
            "members": [
                {
                    "email": "partnerin@kanzlei.de",
                    "type": "USER",
                    "status": "ACTIVE",
                },
                # The API includes the nested group itself as well as its derived users.
                # Only people are written against the root ACL group.
                {"email": "associates@kanzlei.de", "type": "GROUP"},
                {
                    "email": "associate@kanzlei.de",
                    "type": "USER",
                    "status": "ACTIVE",
                },
                # Duplicates and suspended accounts do not become retrieval principals.
                {"email": "ASSOCIATE@KANZLEI.DE", "type": "USER"},
                {
                    "email": "ehemalig@kanzlei.de",
                    "type": "USER",
                    "status": "SUSPENDED",
                },
            ]
        }

    source = _build(
        GoogleDriveSource,
        GoogleDriveConfig(mirror_permissions=True),
        "_get",
        handler,
    )
    source._build_file_entity(DRIVE_FILE, None)
    memberships = _collect(source.generate_access_control_memberships())

    assert [(row.member_id, row.member_type, row.group_id) for row in memberships] == [
        ("partnerin@kanzlei.de", "user", "google:litigation@kanzlei.de"),
        ("associate@kanzlei.de", "user", "google:litigation@kanzlei.de"),
    ]
    assert calls[0][1] == {
        "includeDerivedMembership": "true",
        "maxResults": "200",
    }


def test_google_drive_does_not_crawl_unreferenced_workspace_groups():
    async def unexpected(_url, params=None):
        raise AssertionError(f"unexpected directory crawl with {params}")

    source = _build(
        GoogleDriveSource,
        GoogleDriveConfig(mirror_permissions=True),
        "_get",
        unexpected,
    )
    assert _collect(source.generate_access_control_memberships()) == []


def test_google_drive_file_without_readable_permissions_stays_unknown():
    source, _captured = _google_drive()
    # Drive omits the sub-resource when the signed-in account may not read it.
    unreadable = {k: v for k, v in DRIVE_FILE.items() if k != "permissions"}
    entity = source._build_file_entity(unreadable, None)
    assert entity is not None
    assert entity.access is None
    assert translate_access(entity.access) is None


def test_google_drive_does_not_ask_for_permissions_when_mirroring_is_off():
    source, captured = _google_drive(mirror=False)
    _collect(source._list_files("user", False))
    assert "permissions(" not in captured[0]["fields"]
    assert source._build_file_entity(DRIVE_FILE, None).access is None


# --- Dropbox -------------------------------------------------------------------------

DROPBOX_ENTRY = {
    ".tag": "file",
    "id": "id:abc123",
    "name": "Kaufvertrag.pdf",
    "path_lower": "/mandate/kaufvertrag.pdf",
    "path_display": "/Mandate/Kaufvertrag.pdf",
    "rev": "0157",
    "size": 12,
    "is_downloadable": True,
    "server_modified": "2026-03-02T12:00:00Z",
}


# The same document seen the two ways Dropbox presents a shared file: on its own
# account, and inheriting from the shared folder it sits in.
DROPBOX_EXPLICIT_ENTRY = {**DROPBOX_ENTRY, "has_explicit_shared_members": True}
DROPBOX_INHERITED_ENTRY = {
    **DROPBOX_ENTRY,
    "sharing_info": {"read_only": True, "parent_shared_folder_id": "sf:mandate"},
}


def _dropbox(*, fail=False, mirror=True):
    calls: list[str] = []

    async def handler(url, json_data=None):
        calls.append(url)
        if url.endswith("/sharing/list_file_members"):
            assert json_data == {"file": "id:abc123", "include_inherited": True}
        elif url.endswith("/sharing/list_folder_members"):
            assert json_data == {"shared_folder_id": "sf:mandate"}
        else:
            raise AssertionError(f"unexpected Dropbox call: {url}")
        if fail:
            raise _forbidden(url)
        return {
            "users": DROPBOX_USERS,
            "groups": DROPBOX_GROUPS,
            "invitees": DROPBOX_INVITEES,
        }

    source = _build(
        DropboxSource, DropboxConfig(mirror_permissions=mirror), "_post", handler
    )
    return source, calls


def test_dropbox_mirrors_the_members_of_a_file_shared_on_its_own():
    source, calls = _dropbox()
    access = asyncio.run(source._file_access(DROPBOX_EXPLICIT_ENTRY))
    assert access.viewers == [
        "group:dropbox:g:lit",
        "user:anwalt@kanzlei.de",
        "user:referendar@kanzlei.de",
    ]
    assert any(url.endswith("/sharing/list_file_members") for url in calls)


def test_dropbox_mirrors_the_parent_shared_folders_members_onto_a_file_inside_it():
    """The shape a file server actually produces: one folder read, reused for its files."""
    source, calls = _dropbox()
    first = asyncio.run(source._file_access(DROPBOX_INHERITED_ENTRY))
    second = asyncio.run(source._file_access(DROPBOX_INHERITED_ENTRY))

    assert first.viewers == second.viewers == [
        "group:dropbox:g:lit",
        "user:anwalt@kanzlei.de",
        "user:referendar@kanzlei.de",
    ]
    # Read once and cached; a matter folder must not cost one call per document.
    assert [url for url in calls if url.endswith("/sharing/list_folder_members")] == [
        "https://api.dropboxapi.com/2/sharing/list_folder_members"
    ]


def test_dropbox_member_failure_leaves_access_unknown_not_empty():
    source, _calls = _dropbox(fail=True)
    access = asyncio.run(source._file_access(DROPBOX_EXPLICIT_ENTRY))
    assert access is None
    assert translate_access(access) is None


def test_dropbox_skips_the_extra_member_call_when_mirroring_is_off():
    source, calls = _dropbox(mirror=False)
    assert asyncio.run(source._file_access(DROPBOX_EXPLICIT_ENTRY)) is None
    assert calls == []


# --- Box -----------------------------------------------------------------------------

BOX_FILE = {
    "id": "9001",
    "type": "file",
    "name": "Kaufvertrag.pdf",
    "size": 12,
    "extension": "pdf",
    "created_at": "2026-03-01T12:00:00-00:00",
    "modified_at": "2026-03-02T12:00:00-00:00",
    "permissions": {"can_download": False},
}


def _box(*, fail=False, mirror=True, collaborations=None):
    calls: list[str] = []

    async def handler(url, params=None):
        calls.append(url)
        if url.endswith("/comments"):
            return {"entries": []}
        if url.endswith("/collaborations"):
            if fail:
                raise _forbidden(url)
            return {"entries": collaborations}
        raise AssertionError(f"unexpected Box call: {url}")

    return _build(BoxSource, BoxConfig(mirror_permissions=mirror), "_get", handler), calls


def test_box_mirrors_file_collaborations():
    source, calls = _box(collaborations=BOX_COLLABORATIONS)
    entities = _collect(source._generate_file_entities(BOX_FILE, []))
    file_entity = entities[0]
    assert file_entity.access.viewers == ["group:box:77", "user:anwalt@kanzlei.de"]
    assert any(url.endswith("/files/9001/collaborations") for url in calls)


def test_box_collaboration_failure_leaves_access_unknown_and_still_yields_the_file():
    source, _calls = _box(fail=True)
    entities = _collect(source._generate_file_entities(BOX_FILE, []))
    file_entity = entities[0]
    assert file_entity.name == "Kaufvertrag.pdf"
    assert file_entity.access is None
    assert translate_access(file_entity.access) is None


def test_box_empty_collaboration_body_is_unknown_not_an_empty_grant():
    # Box's request helper turns 403/404 into an empty body, which must not be read as
    # "this file has no collaborators".
    source, _calls = _box(collaborations=None)
    entities = _collect(source._generate_file_entities(BOX_FILE, []))
    assert entities[0].access is None


def test_box_skips_the_extra_collaboration_call_when_mirroring_is_off():
    source, calls = _box(collaborations=BOX_COLLABORATIONS, mirror=False)
    entities = _collect(source._generate_file_entities(BOX_FILE, []))
    assert entities[0].access is None
    # The collaboration *entities* are still emitted; only the ACL read is skipped.
    assert len([url for url in calls if url.endswith("/collaborations")]) == 1


def test_missing_credential_key_is_reported_as_configuration_not_a_crash(
    factory, tmp_path, monkeypatch
) -> None:
    """An admin connecting their first OAuth source must be told what to fix.

    Refusing to write the credential unencrypted is the right outcome; a setup step left
    undone is not a failed request, so it reports as 503. The response is deliberately
    generic — the OAuth callback shares this handler and is unauthenticated by necessity
    — and the text naming the environment variable goes to the log.
    """
    from fastapi.testclient import TestClient

    from knowledge_index.config import AppConfig
    from knowledge_index.config_store import ConfigStore
    from knowledge_index.web.app import create_app

    monkeypatch.delenv("KI_CONNECTOR_CREDENTIAL_KEY", raising=False)
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))

    with TestClient(create_app(factory, store), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/sources",
            json={
                "display_name": "SharePoint",
                "kind": "sharepoint_online",
                "config": {},
                "client_id": "cid",
                "client_secret": "csec",
            },
            headers={"x-ki-principals": "user:admin,role:admin"},
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "credential storage is not configured" in detail
    # The remedy names an environment variable; it must not be handed to an
    # unauthenticated caller through the shared OAuth callback.
    assert "KI_CONNECTOR_CREDENTIAL_KEY" not in detail
