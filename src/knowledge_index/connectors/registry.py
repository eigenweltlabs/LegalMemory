"""The connector catalog and the factory that builds one for a configured source.

This is the seam the whole layer exists for. A connector is described by one
:class:`ConnectorSpec`; building one is ``build_connector(source_record, credentials)``.
Nothing in the pipeline, the engine or the admin UI knows about a specific connector —
they know about the catalog.

Adding a connector is therefore three things and no core edits:

1. add the source class under ``sources/``,
2. add its OAuth settings to ``runtime/providers.yaml`` if it uses OAuth,
3. add one ``ConnectorSpec`` to ``CATALOG`` below.

``mirrors_acls`` is not cosmetic. The permission compiler is fail-closed for every
non-local source, so a connector that cannot read source ACLs produces documents that
nobody can retrieve until an administrator grants access at the project level. The
catalog states this per connector so the admin UI can say it plainly at setup time
rather than leaving an operator to discover an empty search result later.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowledge_index.connectors.bridge import ConnectorAdapter, LoopRunner
from knowledge_index.connectors.runtime import oauth as oauth_runtime
from knowledge_index.connectors.runtime.errors import SourceError
from knowledge_index.connectors.runtime.files import FileService, safe_filename
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.tokens import (
    OAuthTokenProvider,
    StaticTokenProvider,
)


@dataclass(frozen=True)
class ConnectorSpec:
    """Everything the platform needs to know about one connector."""

    short_name: str
    label: str
    category: str
    module: str  # dotted path to the module holding the source class
    class_name: str
    mirrors_acls: bool = False
    incremental: bool = False
    # Whether the connector can enumerate a browsable tree and sync only chosen subtrees.
    # Syncing a firm's entire drive is rarely what anyone wants: it costs conversion and
    # embedding on documents nobody will search for, and indexing a partner's personal
    # files is a different consent conversation than indexing the matter folders.
    supports_scoping: bool = False
    # None for connectors that do not use OAuth (static tokens, app auth).
    oauth_provider: str | None = None
    notes: str = ""
    # A mailbox or personal drive holds one person's correspondence. Granting such a
    # source to a group publishes that person's mail to the firm, so the API refuses a
    # broad default grant on these unless an admin confirms it explicitly.
    private_corpus: bool = False
    config_defaults: dict[str, Any] = field(default_factory=dict)
    # Optional dotted ``module:Class`` implementing provider-event subscriptions. The
    # core event manager imports this seam and knows nothing about Drive, Graph, Slack,
    # or any future provider. Events only wake the connector's normal delta sync.
    event_adapter: str | None = None

    def load(self) -> type:
        module = importlib.import_module(self.module)
        return getattr(module, self.class_name)


SOURCES = "knowledge_index.connectors.sources"

CATALOG: tuple[ConnectorSpec, ...] = (
    ConnectorSpec(
        short_name="sharepoint_online",
        label="SharePoint Online",
        category="Document management",
        module=f"{SOURCES}.sharepoint_online.source",
        class_name="SharePointOnlineSource",
        mirrors_acls=True,
        incremental=True,
        supports_scoping=True,
        oauth_provider="sharepoint_online",
        event_adapter=(
            "knowledge_index.connectors.events.sharepoint:"
            "SharePointEventAdapter"
        ),
        notes=(
            "Mirrors site, library and item permissions and expands Entra and "
            "SharePoint groups, so ethical walls are enforced from the source."
        ),
    ),
    ConnectorSpec(
        short_name="onedrive",
        private_corpus=True,
        label="OneDrive",
        category="File storage",
        module=f"{SOURCES}.onedrive",
        class_name="OneDriveSource",
        mirrors_acls=True,
        incremental=True,
        supports_scoping=True,
        oauth_provider="onedrive",
        event_adapter=(
            "knowledge_index.connectors.events.onedrive:"
            "OneDriveEventAdapter"
        ),
        notes=(
            "Mirrors per-item permissions from Graph, including organization-wide "
            "sharing links, and expands Entra group grants into memberships. Anonymous "
            "links are reported rather than honoured, and an item whose permissions "
            "cannot be read stays fail-closed. Incremental via the drive's delta feed."
        ),
    ),
    ConnectorSpec(
        short_name="clio",
        label="Clio",
        category="Practice management",
        module=f"{SOURCES}.clio",
        class_name="ClioSource",
        mirrors_acls=True,
        incremental=True,
        supports_scoping=True,
        oauth_provider="clio",
        notes=(
            "Indexes the matters and documents the authorizing Clio user can see — a "
            "restricted matter outside that visibility is never fetched. Mirrors "
            "Clio's permission groups: group-restricted matters and documents are "
            "retrievable by that group's members only, unrestricted content by every "
            "authenticated user of this single-firm appliance. Incremental via the "
            "updated-since feed, deletions included. EU region."
        ),
    ),
    ConnectorSpec(
        short_name="netdocuments",
        label="NetDocuments",
        category="Legal DMS",
        module=f"{SOURCES}.netdocuments",
        class_name="NetDocumentsSource",
        mirrors_acls=True,
        incremental=True,
        supports_scoping=True,
        oauth_provider="netdocuments",
        notes=(
            "Indexes the cabinets the authorizing account can open — a cabinet outside "
            "that account's access is never listed. Mirrors cabinet and workspace group "
            "membership, honouring an explicit no-access row as the wall it is, and "
            "expands those groups to their members. A document carrying its own access "
            "list is mirrored to that list; a document whose own list cannot be read "
            "stays fail-closed rather than inheriting its container, because an "
            "override exists in order to be narrower. Incremental via a per-cabinet "
            "modified-since search, with deletions reconciled from the previous run's "
            "ids. Region-bound: the connection's API base URL must match the "
            "repository's region."
        ),
    ),
    ConnectorSpec(
        short_name="teams",
        label="Microsoft Teams",
        category="Messaging",
        module=f"{SOURCES}.teams",
        class_name="TeamsSource",
        mirrors_acls=True,
        oauth_provider="teams",
        notes=(
            "Indexes only the teams and chats reachable by the person who authorizes "
            "the connection. Channel and chat messages are indexed with the audience of the "
            "conversation they were posted in: a standard channel carries the team's "
            "Entra group, which mirrored memberships expand; a private channel and a "
            "chat carry their own members. Membership that cannot be read leaves the "
            "messages fail-closed rather than published to the firm."
        ),
    ),
    ConnectorSpec(
        short_name="slack",
        label="Slack",
        category="Messaging",
        module=f"{SOURCES}.slack",
        class_name="SlackSource",
        mirrors_acls=True,
        incremental=False,
        oauth_provider="slack",
        notes=(
            "Indexes channels in the Slack workspace selected during authorization only "
            "after the bot has been invited to each one. Channel messages are text, with "
            "thread replies folded into the "
            "message that started the thread. A public channel is mirrored as "
            "readable by every authenticated user of this single-firm appliance; a "
            "private channel is included only when the bot was invited, and its "
            "membership is resolved to user "
            "principals, so it stays private. Where membership cannot be read the "
            "messages keep unknown access rather than an empty one, which is "
            "fail-closed. Slack's normal bot token remains usable until it expires or "
            "is revoked; Slack token-rotation apps are not supported yet."
        ),
    ),
    ConnectorSpec(
        short_name="outlook_mail",
        private_corpus=True,
        label="Outlook Mail",
        category="Mail",
        module=f"{SOURCES}.outlook_mail",
        class_name="OutlookMailSource",
        mirrors_acls=True,
        incremental=True,
        oauth_provider="outlook_mail",
        notes=(
            "Indexes the authorizing person's /me mailbox, not the whole tenant and not "
            "delegated/shared mailboxes. The mailbox owner is the only viewer. A mailbox holds one "
            "person's correspondence, so that is the ACL — there are no per-message "
            "permissions to read. If the owner cannot be resolved the mail stays "
            "fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="outlook_calendar",
        label="Outlook Calendar",
        category="Calendar",
        module=f"{SOURCES}.outlook_calendar",
        class_name="OutlookCalendarSource",
        mirrors_acls=True,
        oauth_provider="outlook_calendar",
        notes=(
            "Owner-scoped: the calendar owner is the only viewer. Events belong to one "
            "person's schedule, so that is the ACL. If the owner cannot be resolved the "
            "events stay fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="onenote",
        label="OneNote",
        category="Notes",
        module=f"{SOURCES}.onenote",
        class_name="OneNoteSource",
        mirrors_acls=True,
        oauth_provider="onenote",
        notes=(
            "Owner-scoped: the notebook owner is the only viewer. OneNote exposes no "
            "per-page permissions to a delegated token, but the notebooks belong to one "
            "person, and that is the ACL. If the owner cannot be resolved the pages stay "
            "fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="google_drive",
        label="Google Drive",
        category="File storage",
        module=f"{SOURCES}.google_drive",
        class_name="GoogleDriveSource",
        mirrors_acls=True,
        incremental=True,
        supports_scoping=True,
        oauth_provider="google_drive",
        event_adapter=(
            "knowledge_index.connectors.events.google_drive:"
            "GoogleDriveEventAdapter"
        ),
        notes=(
            "Indexes only files and shared drives reachable by the person who authorizes "
            "the connection; selected folders can narrow that boundary. Incremental via "
            "the Drive changes feed. Mirrors direct-user and domain-wide per-file sharing "
            "permissions requested inline with file metadata; domain-wide sharing "
            "becomes access for every authenticated user of this single-firm appliance "
            "and public links are not honoured. Google Group grants are expanded from "
            "the customer's Admin Directory using a read-only Groups admin privilege; "
            "an external or unreadable group stays fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="google_docs",
        label="Google Docs",
        category="Documents",
        module=f"{SOURCES}.google_docs",
        class_name="GoogleDocsSource",
        mirrors_acls=True,
        incremental=True,
        oauth_provider="google_docs",
        notes=(
            "Mirrors each document's Drive sharing permissions, requested inline with "
            "its metadata so it costs no extra call. Domain-wide sharing becomes "
            "firm-wide access, public links are not honoured, and a document whose "
            "permissions Drive withholds stays fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="gmail",
        private_corpus=True,
        label="Gmail",
        category="Mail",
        module=f"{SOURCES}.gmail",
        class_name="GmailSource",
        mirrors_acls=True,
        incremental=True,
        oauth_provider="gmail",
        notes=(
            "Owner-scoped: the mailbox owner is the only viewer. A mailbox holds one "
            "person's correspondence, so that is the ACL — there are no per-message "
            "permissions to read. If the owner cannot be resolved the mail stays "
            "fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="dropbox",
        label="Dropbox",
        category="File storage",
        module=f"{SOURCES}.dropbox",
        class_name="DropboxSource",
        mirrors_acls=True,
        supports_scoping=True,
        oauth_provider="dropbox",
        notes=(
            "Mirrors per-file sharing members and groups. Outstanding invitations are "
            "not access and are not mirrored; a file whose members cannot be read "
            "stays fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="box",
        label="Box",
        category="File storage",
        module=f"{SOURCES}.box",
        class_name="BoxSource",
        mirrors_acls=True,
        supports_scoping=True,
        oauth_provider="box",
        notes=(
            "Mirrors per-file collaborations for users and groups. Pending invites and "
            "upload-only roles confer no read and are excluded; a file whose "
            "collaborations cannot be read stays fail-closed."
        ),
    ),
    ConnectorSpec(
        short_name="notion",
        label="Notion",
        category="Knowledge base",
        module=f"{SOURCES}.notion",
        class_name="NotionSource",
        oauth_provider="notion",
    ),
    ConnectorSpec(
        short_name="confluence",
        label="Confluence",
        category="Knowledge base",
        module=f"{SOURCES}.confluence",
        class_name="ConfluenceSource",
        mirrors_acls=True,
        oauth_provider="confluence",
        notes=(
            "Mirrors per-page read restrictions for users and groups; comments inherit "
            "the page they sit on. Confluence inverts the usual model — unrestricted "
            "content is readable by anyone who can see the space, and is mirrored as "
            "such rather than as invisible. Content whose restrictions cannot be read "
            "stays fail-closed."
        ),
    ),
)

BY_NAME: dict[str, ConnectorSpec] = {spec.short_name: spec for spec in CATALOG}

# These are the launch connectors currently being worked through end to end. The others
# remain registered and testable through code, but the operator catalog shows them as
# unavailable so nobody mistakes implementation presence for launch readiness.
UI_AVAILABLE = {"sharepoint_online", "google_drive", "onedrive", "clio"}


@dataclass(frozen=True)
class PlannedConnector:
    """A connector on the roadmap: shown in the catalog, not yet buildable.

    A law firm evaluating this appliance judges it by whether it names their DMS.
    The generic file-storage catalog reads as "not built for firms" even though the
    document stores most firms actually run — iManage above all, RA-MICRO in Germany —
    are exactly where this product is heading. These entries put those names on the
    board as visibly planned, with none of the machinery a real ConnectorSpec carries,
    so nothing can mistake a roadmap card for an implementation.
    """

    short_name: str
    label: str
    category: str
    notes: str


PLANNED: tuple[PlannedConnector, ...] = (
    PlannedConnector(
        short_name="imanage",
        label="iManage Work",
        category="Legal DMS",
        notes=(
            "The dominant large-firm legal DMS. Planned: workspace and matter-file "
            "sync with mirrored folder and document security."
        ),
    ),
    PlannedConnector(
        short_name="ra_micro",
        label="RA-MICRO",
        category="Kanzleisoftware",
        notes=(
            "Germany's most widely installed Kanzleisoftware; the E-Akte holds the "
            "firm's matter documents. Planned."
        ),
    ),
    PlannedConnector(
        short_name="datev_anwalt",
        label="DATEV Anwalt",
        category="Kanzleisoftware",
        notes=(
            "DATEV's Kanzlei document store, common where the firm already runs DATEV "
            "for accounting. Planned."
        ),
    ),
    PlannedConnector(
        short_name="annotext",
        label="AnNoText",
        category="Kanzleisoftware",
        notes="Wolters Kluwer Kanzleisoftware for mid-size German firms. Planned.",
    ),
    PlannedConnector(
        short_name="advoware",
        label="advoware",
        category="Kanzleisoftware",
        notes="Kanzleisoftware for small and mid-size German firms. Planned.",
    ),
    PlannedConnector(
        short_name="mycase",
        label="MyCase",
        category="Practice management",
        notes="Cloud practice management for small and mid-size firms. Planned.",
    ),
    PlannedConnector(
        short_name="filevine",
        label="Filevine",
        category="Practice management",
        notes=(
            "Case and document management widely used by plaintiff and mid-size "
            "firms. Planned."
        ),
    ),
    PlannedConnector(
        short_name="smokeball",
        label="Smokeball",
        category="Practice management",
        notes="Practice management with document automation for small firms. Planned.",
    ),
    PlannedConnector(
        short_name="opentext_edocs",
        label="OpenText eDOCS",
        category="Legal DMS",
        notes="Established DMS in legal and public-sector practices. Planned.",
    ),
    PlannedConnector(
        short_name="worldox",
        label="Worldox",
        category="Legal DMS",
        notes=(
            "Long-standing small and mid-firm DMS, now part of NetDocuments. Planned "
            "for firms still running it on-premises."
        ),
    ),
    PlannedConnector(
        short_name="epona_dmsforlegal",
        label="Epona DMSforLegal",
        category="Legal DMS",
        notes="SharePoint-based legal DMS common in European firms. Planned.",
    ),
    PlannedConnector(
        short_name="highq",
        label="HighQ",
        category="Legal DMS",
        notes=(
            "Thomson Reuters collaboration and deal-room platform. Planned: site and "
            "file sync with mirrored membership."
        ),
    ),
)


def catalog() -> list[dict]:
    """The catalog as plain dicts for the admin UI: built connectors, then the roadmap."""
    built = [
        {
            "id": spec.short_name,
            "name": spec.label,
            "category": spec.category,
            "acl_sync": spec.mirrors_acls,
            "supports_scoping": spec.supports_scoping,
            "private_corpus": spec.private_corpus,
            "incremental": "native delta" if spec.incremental else "full rescan",
            "event_driven": bool(spec.event_adapter),
            "auth": ["OAuth"] if spec.oauth_provider else ["credentials"],
            "needs_oauth": bool(spec.oauth_provider),
            "connectable": spec.short_name in UI_AVAILABLE,
            "notes": spec.notes,
        }
        for spec in sorted(CATALOG, key=lambda item: item.label)
    ]
    # Capability fields are None, not False: a roadmap entry has no implementation to
    # be honest about, and claiming "no permission mirror" for a connector that does
    # not exist yet would be a statement about nothing. Tuple order is prominence —
    # the DMS a firm looks for first comes first — so it is not re-sorted here.
    planned = [
        {
            "id": item.short_name,
            "name": item.label,
            "category": item.category,
            "acl_sync": None,
            "supports_scoping": False,
            "private_corpus": False,
            "incremental": None,
            "event_driven": False,
            "auth": [],
            "needs_oauth": False,
            "connectable": False,
            "planned": True,
            "notes": item.notes,
        }
        for item in PLANNED
    ]
    return built + planned


def get(short_name: str) -> ConnectorSpec:
    try:
        return BY_NAME[short_name]
    except KeyError:
        raise SourceError(
            f"no connector named {short_name!r}. Installed: {', '.join(sorted(BY_NAME))}"
        ) from None


def build_connector(
    *,
    short_name: str,
    config: dict | None = None,
    credentials: dict | None = None,
    cursor_data: dict | None = None,
    node_selections: list | None = None,
    persist_credentials: Callable[[dict], Any] | None = None,
    file_service: FileService | None = None,
    staging_dir: str | None = None,
    run_id: str = "sync",
    allow_private_hosts: bool = False,
) -> ConnectorAdapter:
    """Instantiate a connector ready for the sync engine to drive."""
    spec = get(short_name)
    source_class = spec.load()
    logger = ContextualLogger(source=short_name, run_id=run_id)
    http_client = HttpClient(allow_private_hosts=allow_private_hosts)
    files = file_service or FileService(
        staging_dir or _default_staging_dir(), run_id=run_id
    )

    auth = _build_auth(spec, credentials or {}, persist_credentials)
    config_model = _build_config(source_class, {**spec.config_defaults, **(config or {})})
    cursor = _build_cursor(source_class, cursor_data)

    # The source is constructed on the same loop it will be driven on. Building it with
    # asyncio.run() would create and immediately close a loop, leaving any primitive the
    # connector allocated in create() bound to a dead one.
    runner = LoopRunner()
    try:
        source = runner.run(
            source_class.create(
                auth=auth, logger=logger, http_client=http_client, config=config_model
            )
        )
    except BaseException:
        runner.close()
        raise
    return ConnectorAdapter(
        short_name,
        source,
        file_service=files,
        cursor=cursor,
        node_selections=node_selections,
        runner=runner,
        http_client=http_client,
        logger=logger,
    )


def _default_staging_dir() -> str:
    """Where staged content lives when the caller does not say.

    Deliberately under the artifact directory rather than a temp path: staged content has
    to survive the scan process so the fetch stage can read it later.
    """
    import os

    # The container sets /data/artifacts explicitly. A local CLI/test process must not
    # silently attempt to create /data (root-owned or read-only); match AppConfig's local
    # default when the deployment variable is absent.
    return os.environ.get("KI_ARTIFACT_DIR", ".ki/artifacts") + "/connector-staging"


def staging_root_for_source(source_id: str) -> Path:
    """The directory one source's connector stages its copies of the firm's files under.

    Public because deleting a connection has to reclaim precisely this tree. Derived from
    the same two pieces ``build_connector`` uses — ``connector_from_source`` passes the
    source id as the run id — so the two cannot drift into a delete that misses.
    """
    return Path(_default_staging_dir()) / safe_filename(source_id)


def _build_auth(
    spec: ConnectorSpec,
    credentials: dict,
    persist: Callable[[dict], Any] | None,
):
    if not spec.oauth_provider:
        token = credentials.get("access_token") or credentials.get("token")
        if not token:
            raise SourceError(
                f"{spec.short_name} needs a token; none was stored for this connection"
            )
        return StaticTokenProvider(str(token))

    provider = oauth_runtime.get_provider(spec.oauth_provider)
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")

    async def refresh(refresh_token_value: str):
        if not client_id or not client_secret:
            raise SourceError(
                f"{spec.short_name} cannot refresh: the connection has no OAuth client "
                "credentials. Re-run setup with the firm's own client id and secret."
            )
        refreshed = await oauth_runtime.refresh_token(
            provider,
            refresh_token=refresh_token_value,
            client_id=str(client_id),
            client_secret=str(client_secret),
        )
        return (
            refreshed["access_token"],
            refreshed.get("refresh_token"),
            refreshed.get("expires_in"),
        )

    async def persist_refreshed(updated: dict) -> None:
        if persist is None:
            return
        merged = {**credentials, **updated}
        result = persist(merged)
        if hasattr(result, "__await__"):
            await result

    return OAuthTokenProvider(
        credentials,
        oauth_type=provider.oauth_type,
        refresh=refresh,
        persist=persist_refreshed,
        source_short_name=spec.short_name,
    )


def _build_config(source_class: type, values: dict):
    """Build the connector's typed config, or a permissive stand-in if it has none."""
    config_class = getattr(source_class, "config_class", None)
    if config_class is None:
        return _EmptyConfig()
    return config_class(**values)


class _EmptyConfig:
    """Stand-in for connectors that declare no config class."""

    def __getattr__(self, name: str):
        raise AttributeError(name)


def _build_cursor(source_class: type, cursor_data: dict | None):
    cursor_class = getattr(source_class, "cursor_class", None)
    if cursor_class is None:
        return None
    from uuid import uuid4

    from knowledge_index.connectors.cursors.state import SyncCursor

    return SyncCursor(uuid4(), cursor_class, cursor_data)
