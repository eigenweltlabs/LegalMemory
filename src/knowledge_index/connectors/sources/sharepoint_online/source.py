"""SharePoint Online source.

Syncs data from SharePoint Online via Microsoft Graph API.

Entity hierarchy:
- Sites - discovered via search or explicit URL
- Drives - document libraries within each site
- Items/Files - content within each drive
- Pages - site pages (optional)
- Lists/ListItems - non-document-library lists

Access graph generation:
- Extracts permissions from drive items via Graph API
- Expands Entra ID groups via /groups/{id}/members
- Expands SP site groups via SharePoint REST API (requires SP-scoped token)
- Maps to canonical principal format: user:{email}, group:entra:{id}, group:sp:{name}

Incremental sync:
- Uses Graph delta queries (/drives/{id}/root/delta)
- Per-drive delta tokens stored in cursor

One source variant ships: SharePointOnlineSource (OAuth, delegated user auth).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.types import MembershipTuple
from knowledge_index.connectors.runtime.types import BrowseNode, NodeSelectionData
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.runtime.errors import EntityProcessingError
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.configs import SharePointOnlineConfig
from knowledge_index.connectors.cursors.sharepoint_online import SharePointOnlineCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.sharepoint_online import (
    SharePointOnlineFileDeletionEntity,
)
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.sources.microsoft_sensitivity_labels import SensitivityLabelFilter
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.sources.sharepoint_online.acl import extract_access_control
from knowledge_index.connectors.sources.sharepoint_online.builders import (
    build_drive_entity,
    build_file_entity,
    build_page_entity,
    build_site_entity,
)
from knowledge_index.connectors.sources.sharepoint_online.client import GRAPH_BASE_URL, GraphClient
from knowledge_index.connectors.sources.sharepoint_online.graph_groups import EntraGroupExpander
from knowledge_index.connectors.runtime.types import AuthenticationMethod, OAuthType

MAX_CONCURRENT_FILE_DOWNLOADS = 10
ITEM_BATCH_SIZE = 50

# Synthetic principal representing the SharePoint "Everyone except external users"
# claim. SP exposes this claim as a member of site groups but our membership
# table only handles real users / Entra groups / SP groups. We translate the
# claim into a synthetic group, populate it with the tenant's internal members
# at sync time, and let the broker's recursive expansion do the rest.
EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL = "claim:everyone_except_external"
EVERYONE_EXCEPT_EXTERNAL_DISPLAY_NAME = "Everyone except external users (synthetic)"


# Hosts the firm's Microsoft token may be sent to. Graph for the API, and the
# tenant's own *.sharepoint.com for the SharePoint REST token; a pre-authenticated
# download URL carries its own credential and is fetched without ours.
SHAREPOINT_AUTH_HOSTS = ("graph.microsoft.com", "sharepoint.com")


@dataclass
class PendingFileDownload:
    """Holds a file entity that needs its content downloaded."""

    entity: Any
    drive_id: str
    item_id: str


# =============================================================================
# Base class — shared sync, browse tree, download, and ACL logic
# =============================================================================


class SharePointOnlineBase(BaseSource):
    """Shared implementation for SharePoint Online sources.

    Subclasses must implement the auth-specific hooks:
    - create() — class constructor
    - _get_access_token() — return a valid Microsoft Graph token
    - _handle_401() — refresh/re-exchange on 401, return new token
    - _make_sp_token_provider_for_site(site_url) — per-site SP REST token provider
    - _get_download_auth(url) — auth suitable for file download
    - _discover_sites(graph_client) — site discovery strategy
    """

    # Instance attributes set by _init_common()
    _site_url: str
    _include_personal_sites: bool
    _include_pages: bool
    _item_level_entra_groups: Set[str]
    # Site-scoped SP group tracking: {site_url: {sp_group_name, ...}}
    # Keyed by normalized site URL so multi-site syncs can expand SP groups per site.
    _item_level_sp_groups: Dict[str, Set[str]]
    # Set to True during membership extraction when an SP group contains the
    # "Everyone except external users" claim, so we know to enumerate internal
    # tenant users once at the end.
    _needs_internal_user_enum: bool

    def _init_common(self, config: SharePointOnlineConfig) -> None:
        """Initialize fields shared by both OAuth and client-credentials sources."""
        self._site_url = config.site_url.rstrip("/") if config.site_url else ""
        self._include_personal_sites = config.include_personal_sites
        self._include_pages = config.include_pages
        self._item_level_entra_groups = set()
        self._item_level_sp_groups = {}
        self._needs_internal_user_enum = False
        self._excluded_sensitivity_label_ids = list(config.excluded_sensitivity_label_ids)
        self._skip_encrypted_files = config.skip_encrypted_files
        self._skip_unlabeled_files = config.skip_unlabeled_files
        self._label_filter: Optional[SensitivityLabelFilter] = None

    # -- Auth hooks (subclasses override) --

    async def _get_access_token(self) -> str:
        """Get a valid Microsoft Graph access token."""
        raise NotImplementedError

    async def _handle_401(self) -> str:
        """Handle a 401 by refreshing/re-exchanging. Returns new token."""
        raise NotImplementedError

    def _make_sp_token_provider_for_site(self, site_url: str) -> Optional[Callable]:
        """Create an SP REST token provider scoped to a specific site URL.

        Subclasses must override. Returns None if a token cannot be obtained
        for the given site (e.g., malformed URL).
        """
        raise NotImplementedError

    async def _get_download_auth(self, url: str) -> Any:
        """Return an auth object suitable for FileService.download_from_url."""
        return self.auth

    async def _discover_sites(self, graph_client: GraphClient) -> List[Dict[str, Any]]:
        """Discover SharePoint sites to sync."""
        raise NotImplementedError

    @property
    def _delta_prefer_headers(self) -> List[str]:
        """Prefer headers for delta queries (permission change tracking)."""
        # These headers are supported by SharePoint/OneDrive driveItem delta and are not
        # limited to app-only auth. Without them an event could wake the delta feed for a
        # permission revocation and Graph would return no item, leaving access alive until
        # the periodic full scan.
        return [
            "deltashowsharingchanges",
            "deltashowremovedasdeleted",
            "deltatraversepermissiongaps",
        ]

    # -- Shared client factories --

    def _create_graph_client(self) -> GraphClient:
        return GraphClient(
            access_token_provider=self._get_access_token,
            http_client=self.http_client,
            logger=self.logger,
        )

    def _create_group_expander(self) -> EntraGroupExpander:
        return EntraGroupExpander(
            access_token_provider=self._get_access_token,
            http_client=self.http_client,
            logger=self.logger,
        )

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Make an authenticated GET request to Microsoft Graph API."""
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401:
            self.logger.warning("Received 401 from Microsoft Graph API — refreshing token")
            new_token = await self._handle_401()
            headers = {"Authorization": f"Bearer {new_token}", "Accept": "application/json"}
            response = await self.http_client.get(url, headers=headers, params=params)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    def _derive_sp_hostname(self) -> Optional[str]:
        """Derive the SharePoint hostname from the site URL."""
        if not self._site_url:
            return None
        parsed = urlparse(self._site_url)
        return parsed.netloc or None

    @staticmethod
    def _normalize_site_url(site_url: str) -> str:
        """Normalize a site URL for use as a dict key (strip trailing slash)."""
        return (site_url or "").rstrip("/")

    @staticmethod
    def _parse_browse_node_id(source_node_id: str) -> dict:
        """Recover the ids a browse node encodes, as a fallback when metadata is missing.

        Browse ids are self-describing — ``site:{site}``, ``drive:{site}|{drive}``,
        ``folder:{drive}|{folder}``, ``file:{drive}|{item}``. Parsing them means a stored
        selection still resolves even if the metadata a caller round-tripped was dropped,
        which otherwise degrades to a scoped sync that silently indexes nothing.
        """
        prefix, _, payload = (source_node_id or "").partition(":")
        if not payload:
            return {}
        if prefix == "site":
            return {"site_id": payload}
        parts = payload.split("|")
        if prefix == "drive" and len(parts) == 2:
            return {"site_id": parts[0], "drive_id": parts[1]}
        if prefix in ("folder", "file") and len(parts) == 2:
            return {"drive_id": parts[0], "node_id": parts[1]}
        return {}

    def _track_entity_groups(self, entity: BaseEntity, site_url: str = "") -> None:
        """Track Entra ID and SP site groups found in entity permissions.

        Args:
            entity: The entity whose access viewers to inspect.
            site_url: The site URL this entity belongs to. SP groups are keyed
                by site URL so multi-site syncs can expand SP groups per-site.
                May be empty for paths that lack site context (incremental /
                targeted single-file); those SP groups won't expand.
        """
        if not hasattr(entity, "access") or entity.access is None:
            return
        norm_site = self._normalize_site_url(site_url)
        for viewer in entity.access.viewers or []:
            if viewer.startswith("group:entra:"):
                group_id = viewer[len("group:") :]
                self._item_level_entra_groups.add(group_id)
            elif viewer.startswith("group:sp:"):
                sp_name = viewer[len("group:") :]
                self._item_level_sp_groups.setdefault(norm_site, set()).add(sp_name)

    # -- SP site group membership parsing --

    # Match regular user logins: "i:0#.f|membership|<email>"
    _MEMBERSHIP_LOGIN_RE = re.compile(r"^i:0#\.f\|membership\|(?P<email>[^|]+@[^|]+)$")
    # Match Entra federated group logins: "c:0o.c|federateddirectoryclaimprovider|<guid>[_o]"
    _ENTRA_GROUP_LOGIN_RE = re.compile(
        r"^c:0o\.c\|federateddirectoryclaimprovider\|"
        r"(?P<guid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(_o)?$"
    )
    # Match the "Everyone except external users" claim:
    #   "c:0-.f|rolemanager|spo-grid-all-users/<tenantId>"
    # PrincipalType=4 (SecurityGroup), but the claim provider is `rolemanager`
    # rather than `federateddirectoryclaimprovider`. Represents all tenant
    # users with userType=Member; excludes B2B guests by definition.
    _EVERYONE_EXCEPT_EXTERNAL_LOGIN_RE = re.compile(
        r"^c:0-\.f\|rolemanager\|spo-grid-all-users/[0-9a-fA-F-]+$"
    )

    @classmethod
    def _email_from_membership_login(cls, login: str) -> Optional[str]:
        """Extract email from SP user LoginName if it follows the membership pattern.

        Only matches "i:0#.f|membership|<email>". Returns None for role principals
        (e.g., "c:0-.f|rolemanager|spo-grid-all-users/...") and other shapes so
        we don't pollute the membership table with fake email-like strings.
        """
        if not login:
            return None
        m = cls._MEMBERSHIP_LOGIN_RE.match(login)
        if m:
            return m.group("email").strip().lower() or None
        return None

    @classmethod
    def _parse_sp_group_member(cls, user: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Parse one entry from /_api/web/sitegroups({id})/users into (member_id, member_type).

        Returns None for entries that should not become memberships:
        - Catch-all "All" / "Everyone" principals (PrincipalType=15)
        - DistList, SPGroup, RoleManager (other than the recognized claim below)
        - Unparseable entries (no email for users, no GUID for groups)

        Recognized PrincipalType=4 shapes:
        - Entra federated group: ``c:0o.c|federateddirectoryclaimprovider|<guid>[_o]``
          → returns ``("entra:<guid>", "group")``.
        - "Everyone except external users" claim:
          ``c:0-.f|rolemanager|spo-grid-all-users/<tenantId>`` → returns the
          synthetic ``(EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL, "group")`` sentinel.
          The caller (``_expand_sp_site_groups``) then enumerates internal
          tenant users once per sync to populate the synthetic group.
        - Any other PT=4 LoginName: returns None. The caller logs the raw
          shape at info-level so unknown claim shapes show up in operator
          logs and can be wired up explicitly later.

        PrincipalType reference:
            1  = User
            2  = DistList
            4  = SecurityGroup (Entra group OR rolemanager claim)
            8  = SPGroup
            15 = All
            16 = RoleManager
        """
        ptype = user.get("PrincipalType")
        login = user.get("LoginName", "") or ""

        if ptype == 1:
            email = user.get("Email") or ""
            email = email.strip().lower()
            if not email:
                email = cls._email_from_membership_login(login) or ""
            if not email:
                return None
            # Bare email (no "user:" prefix) matches the broker storage
            # convention used by EntraGroupExpander and SP 2019 V2.
            return (email, "user")

        if ptype == 4:
            m = cls._ENTRA_GROUP_LOGIN_RE.match(login)
            if m:
                return (f"entra:{m.group('guid').lower()}", "group")
            if cls._EVERYONE_EXCEPT_EXTERNAL_LOGIN_RE.match(login):
                return (EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL, "group")
            return None

        # PrincipalType 2 (DistList), 8 (SPGroup), 15 (All), 16 (RoleManager),
        # and unknown types are intentionally skipped.
        return None

    @classmethod
    def _is_unrecognized_pt4_login(cls, user: Dict[str, Any]) -> bool:
        """Return True for a PT=4 entry whose LoginName matches none of our patterns.

        Used at the call site to emit a one-line diagnostic so that unknown
        claim shapes (rare custom rolemanager roles, legacy Windows claims,
        etc.) surface in operator logs without breaking sync.
        """
        if user.get("PrincipalType") != 4:
            return False
        login = user.get("LoginName", "") or ""
        return not (
            cls._ENTRA_GROUP_LOGIN_RE.match(login)
            or cls._EVERYONE_EXCEPT_EXTERNAL_LOGIN_RE.match(login)
        )

    # -- Browse Tree --

    BROWSE_TREE_MAX_ITEMS = 500

    def parse_browse_node_id(self, node_id: str) -> tuple:
        """Parse an encoded browse node ID into (node_type, metadata_dict).

        Encoding conventions (defined by get_browse_children):
        - "site:{site_id}"
        - "drive:{site_id}|{drive_id}"
        - "folder:{drive_id}|{folder_id}"
        """
        if ":" not in node_id:
            return "unknown", {"raw_id": node_id}

        prefix, _, payload = node_id.partition(":")
        if prefix == "site":
            return "site", {"site_id": payload}
        elif prefix == "drive":
            parts = payload.split("|", 1)
            return "drive", {
                "site_id": parts[0],
                "drive_id": parts[1] if len(parts) > 1 else "",
            }
        elif prefix == "folder":
            parts = payload.split("|", 1)
            return "folder", {
                "drive_id": parts[0],
                "folder_id": parts[1] if len(parts) > 1 else "",
            }
        else:
            return prefix, {"raw_id": node_id}

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """Lazy-load tree nodes from Microsoft Graph API."""
        graph_client = self._create_graph_client()
        nodes: List[BrowseNode] = []

        if parent_node_id is None:
            sites = await self._discover_sites(graph_client)
            for site in sites:
                site_id = site.get("id", "")
                nodes.append(
                    BrowseNode(
                        source_node_id=f"site:{site_id}",
                        node_type="site",
                        title=site.get("displayName", site_id),
                        description=site.get("description"),
                        has_children=True,
                        node_metadata={
                            "site_id": site_id,
                            "web_url": site.get("webUrl", ""),
                        },
                    )
                )

        elif parent_node_id.startswith("site:"):
            site_id = parent_node_id[5:]

            async for drive in graph_client.get_drives(site_id):
                drive_id = drive.get("id", "")
                nodes.append(
                    BrowseNode(
                        source_node_id=f"drive:{site_id}|{drive_id}",
                        node_type="drive",
                        title=drive.get("name", drive_id),
                        description=drive.get("description"),
                        has_children=True,
                        node_metadata={
                            "site_id": site_id,
                            "drive_id": drive_id,
                            "drive_type": drive.get("driveType", ""),
                        },
                    )
                )

        elif parent_node_id.startswith("drive:"):
            payload = parent_node_id[6:]
            if "|" not in payload:
                raise ValueError(
                    f"Malformed drive node ID: expected 'drive:{{site_id}}|{{drive_id}}', "
                    f"got '{parent_node_id}'"
                )
            _site_id, drive_id = payload.split("|", 1)
            await self._browse_drive_children(graph_client, drive_id, "root", nodes, _site_id)

        elif parent_node_id.startswith("folder:"):
            payload = parent_node_id[7:]
            if "|" not in payload:
                raise ValueError(
                    f"Malformed folder node ID: expected 'folder:{{drive_id}}|{{folder_id}}', "
                    f"got '{parent_node_id}'"
                )
            drive_id, folder_id = payload.split("|", 1)
            await self._browse_drive_children(graph_client, drive_id, folder_id, nodes)

        else:
            raise ValueError(
                f"Unrecognized browse node ID prefix: '{parent_node_id}'. "
                f"Expected 'site:', 'drive:', or 'folder:'."
            )

        return nodes

    async def _browse_drive_children(
        self,
        graph_client: GraphClient,
        drive_id: str,
        folder_id: str,
        nodes: List[BrowseNode],
        site_id: str = "",
    ) -> None:
        """Populate nodes list with immediate children of a drive folder.

        ``site_id`` is carried onto every child so a selected folder stays associated with
        its site; without it a folder selection cannot be matched against a site selection.
        """
        count = 0
        async for item in graph_client.get_drive_children(drive_id, folder_id):
            if count >= self.BROWSE_TREE_MAX_ITEMS:
                break

            item_id = item.get("id", "")
            name = item.get("name", "")

            if item.get("folder"):
                child_count = item["folder"].get("childCount", 0)
                nodes.append(
                    BrowseNode(
                        source_node_id=f"folder:{drive_id}|{item_id}",
                        node_type="folder",
                        title=name,
                        item_count=child_count,
                        has_children=child_count > 0,
                        node_metadata={
                            "site_id": site_id,
                            "drive_id": drive_id,
                            "folder_id": item_id,
                        },
                    )
                )
            elif item.get("file"):
                nodes.append(
                    BrowseNode(
                        source_node_id=f"file:{drive_id}|{item_id}",
                        node_type="file",
                        title=name,
                        has_children=False,
                        node_metadata={
                            "site_id": site_id,
                            "drive_id": drive_id,
                            "item_id": item_id,
                            "mime_type": item.get("file", {}).get("mimeType", ""),
                            "size": item.get("size", 0),
                        },
                    )
                )

            count += 1

    # -- File Download --

    async def _download_and_save_file(
        self,
        entity: Any,
        files: FileService,
        drive_id: str,
        item_id: str,
    ) -> Any:
        """Download file content and save via FileService."""
        graph_client = self._create_graph_client()
        try:
            download_url = await graph_client.get_file_content_url(drive_id, item_id)
            if download_url:
                entity.url = download_url
            elif not entity.url or "graph.microsoft.com" not in entity.url:
                entity.url = (
                    f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content"
                )

            auth = await self._get_download_auth(entity.url)

            await files.download_from_url(
                entity=entity,
                client=self.http_client,
                auth=auth,
                logger=self.logger,
                auth_hosts=SHAREPOINT_AUTH_HOSTS,
            )
            return entity
        except FileSkippedException:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            self.logger.warning(f"Failed to download file {entity.file_name}: {e}")
            raise EntityProcessingError(f"Failed to download file {entity.file_name}: {e}") from e

    async def _download_files_parallel(
        self, pending: List[PendingFileDownload], files: FileService
    ) -> List[BaseEntity]:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FILE_DOWNLOADS)
        results: List[BaseEntity] = []

        async def download_one(item: PendingFileDownload):
            async with semaphore:
                try:
                    entity = await self._download_and_save_file(
                        item.entity,
                        files,
                        item.drive_id,
                        item.item_id,
                    )
                    results.append(entity)
                except FileSkippedException:
                    self.logger.debug(f"File download skipped for {item.drive_id}/{item.item_id}")
                except EntityProcessingError as e:
                    self.logger.warning(f"Skipping file download: {e}")
                except Exception as e:
                    self.logger.warning(f"Unexpected error downloading {item.entity.name}: {e}")

        tasks = [asyncio.create_task(download_one(p)) for p in pending]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    # -- Sync Decision --

    def _should_do_full_sync(self, cursor: SyncCursor | None) -> tuple:
        cursor_data = cursor.data if cursor else {}
        if not cursor_data:
            return True, "no cursor data (first sync)"

        schema = SharePointOnlineCursor(**cursor_data)
        if schema.needs_full_sync():
            return True, "full_sync_required flag set or no delta tokens"

        if schema.needs_periodic_full_sync():
            return True, "periodic full sync needed (>7 days since last)"

        return False, "incremental sync (valid delta tokens)"

    # -- Entity Generation --

    async def generate_entities(  # noqa: C901
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all SharePoint entities using full, incremental, or targeted sync."""
        cursor_data = cursor.data if cursor else {}
        for g in cursor_data.get("tracked_entra_groups", []):
            self._item_level_entra_groups.add(g)

        # tracked_sp_groups format changed from List[str] (flat names) to
        # Dict[site_url, List[str]] (site-scoped). Migrate defensively.
        tracked_sp = cursor_data.get("tracked_sp_groups")
        if isinstance(tracked_sp, dict):
            for site_url, names in tracked_sp.items():
                if isinstance(names, list):
                    self._item_level_sp_groups[site_url] = set(names)
        elif isinstance(tracked_sp, list):
            self.logger.info(
                "Legacy tracked_sp_groups list format detected; discarding — "
                "will re-collect on next full sync"
            )

        if node_selections:
            is_full, reason = self._should_do_full_sync(cursor)
            self.logger.info(
                f"Sync strategy: TARGETED {'FULL' if is_full else 'INCREMENTAL'} "
                f"({len(node_selections)} roots; {reason})"
            )
            if is_full:
                async for entity in self._targeted_sync(cursor, files, node_selections):
                    yield entity
            else:
                async for entity in self._incremental_sync(
                    cursor, files, node_selections=node_selections
                ):
                    yield entity
            return

        is_full, reason = self._should_do_full_sync(cursor)
        self.logger.info(f"Sync strategy: {'FULL' if is_full else 'INCREMENTAL'} ({reason})")

        if is_full:
            async for entity in self._full_sync(cursor, files):
                yield entity
        else:
            async for entity in self._incremental_sync(cursor, files):
                yield entity

    async def _resolve_unresolved_viewers(
        self, entity: BaseEntity, graph_client: GraphClient
    ) -> None:
        """Resolve any user:id:{uuid} viewers to user:{email}."""
        if not hasattr(entity, "access") or entity.access is None:
            return
        viewers = entity.access.viewers or []
        unresolved = [v for v in viewers if v.startswith("user:id:")]
        if not unresolved:
            return
        user_ids = [v[len("user:id:") :] for v in unresolved]
        resolved = await graph_client.resolve_user_ids(user_ids)
        new_viewers = []
        for v in viewers:
            if v.startswith("user:id:"):
                uid = v[len("user:id:") :]
                email = resolved.get(uid)
                if email:
                    new_viewers.append(f"user:{email}")
                    continue
                self.logger.warning(f"Dropping unresolvable user viewer: {v}")
            else:
                new_viewers.append(v)
        entity.access.viewers = new_viewers

    @staticmethod
    def _has_link_permission(permissions: List[Dict[str, Any]]) -> bool:
        """Return True if any permission carries a sharing-link block."""
        return any(p.get("link") for p in (permissions or []))

    def _get_label_filter(self) -> Optional[SensitivityLabelFilter]:
        """Lazily build a Purview sensitivity-label filter from config.

        Returns None when no filtering is configured.
        """
        if self._label_filter is not None:
            return self._label_filter
        if not self._excluded_sensitivity_label_ids and not self._skip_unlabeled_files:
            return None
        self._label_filter = SensitivityLabelFilter(
            excluded_label_ids=self._excluded_sensitivity_label_ids,
            skip_encrypted=self._skip_encrypted_files,
            skip_unlabeled=self._skip_unlabeled_files,
            http_client=self.http_client,
            token_provider=self._get_access_token,
            logger=self.logger,
        )
        return self._label_filter

    @staticmethod
    def _extract_group_id_from_drives(drives: List[Dict[str, Any]]) -> Optional[str]:
        """Pull the backing M365 Group ID off any drive in the site, if present.

        Group-connected SharePoint sites surface their group as
        ``drive.owner.group.id``. Non-group sites (classic comms sites, etc.)
        won't have this; the site short-circuit is skipped in that case.
        """
        for drive in drives:
            owner = drive.get("owner") or {}
            group = owner.get("group") if isinstance(owner, dict) else None
            raw_id = group.get("id") if isinstance(group, dict) else None
            if isinstance(raw_id, str) and raw_id:
                return raw_id
        return None

    async def _full_sync(  # noqa: C901
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
    ) -> AsyncGenerator[BaseEntity, None]:
        entity_count = 0
        graph_client = self._create_graph_client()
        label_filter = self._get_label_filter()

        sites = await self._discover_sites(graph_client)

        for site_data in sites:
            site_id = site_data.get("id", "")
            site_url = self._normalize_site_url(site_data.get("webUrl", ""))

            # Collect all drives for this site (single API call)
            all_drives = []
            async for drive_data in graph_client.get_drives(site_id):
                all_drives.append(drive_data)

            # Container-label short-circuit: if the site's backing M365 Group
            # carries a blocked Purview label, skip the whole site before we
            # walk any drives.
            if label_filter is not None:
                group_id = self._extract_group_id_from_drives(all_drives)
                if await label_filter.should_skip_site(site_id=site_id, group_id=group_id):
                    continue

            # Fetch site-level permissions from the first drive's root.
            site_access = None
            if all_drives:
                try:
                    site_permissions = await graph_client.get_drive_root_permissions(
                        all_drives[0]["id"]
                    )
                    site_access = await extract_access_control(site_permissions)
                except Exception as e:
                    self.logger.warning(f"Could not fetch site-level permissions: {e}")

            try:
                site_entity = await build_site_entity(site_data, [], access=site_access)
                self._track_entity_groups(site_entity, site_url)
                yield site_entity
                entity_count += 1

                site_breadcrumb = Breadcrumb(
                    entity_id=site_entity.site_id,
                    name=site_entity.display_name,
                    entity_type="SharePointOnlineSiteEntity",
                )
                site_breadcrumbs = [site_breadcrumb]
            except EntityProcessingError as e:
                self.logger.warning(f"Skipping site {site_id}: {e}")
                continue

            for drive_data in all_drives:
                drive_id = drive_data.get("id", "")
                try:
                    # Each drive gets its own root permissions
                    drive_access = site_access
                    if drive_id != all_drives[0]["id"]:
                        try:
                            drive_permissions = await graph_client.get_drive_root_permissions(
                                drive_id
                            )
                            drive_access = await extract_access_control(drive_permissions)
                        except Exception:
                            pass  # Fall back to site_access

                    drive_entity = await build_drive_entity(
                        drive_data, site_id, site_breadcrumbs, access=drive_access
                    )
                    self._track_entity_groups(drive_entity, site_url)
                    yield drive_entity
                    entity_count += 1

                    drive_breadcrumb = Breadcrumb(
                        entity_id=drive_entity.drive_id,
                        name=drive_entity.name,
                        entity_type="SharePointOnlineDriveEntity",
                    )
                    drive_breadcrumbs = site_breadcrumbs + [drive_breadcrumb]

                    pending_files: List[PendingFileDownload] = []

                    async for item_data in graph_client.get_drive_items_recursive(drive_id):
                        if item_data.get("folder"):
                            continue

                        if item_data.get("file"):
                            if label_filter is not None and await label_filter.should_skip_item(
                                drive_id=drive_id,
                                item_id=item_data["id"],
                                item_name=item_data.get("name", ""),
                            ):
                                continue
                            try:
                                # A permission read that fails must not lose the document.
                                # Dropping it would leave the object absent from the scan,
                                # and a full sync tombstones whatever it does not see — so a
                                # transient 403 would delete real documents from the index.
                                # Passing permissions=None yields the file with an unknown
                                # ACL instead, which is fail-closed but recoverable.
                                try:
                                    permissions = await graph_client.get_item_permissions(
                                        drive_id,
                                        item_data["id"],
                                    )
                                except SourceAuthError:
                                    raise
                                except Exception as exc:
                                    self.logger.warning(
                                        f"Could not read permissions for "
                                        f"{item_data.get('name', item_data['id'])}: {exc}. "
                                        "Indexing it with an unknown ACL (not retrievable "
                                        "until permissions can be read or a local grant is "
                                        "added)."
                                    )
                                    permissions = None

                                # Sharing-link permissions need the file's SP UniqueId
                                # to translate into the SharingLinks.* SP site group.
                                # Skip the extra fetch when the file has no sharing links.
                                sp_unique_id = None
                                if self._has_link_permission(permissions):
                                    sp_unique_id = await graph_client.get_item_sp_unique_id(
                                        drive_id, item_data["id"]
                                    )

                                file_entity = await build_file_entity(
                                    item_data,
                                    drive_id,
                                    site_id,
                                    drive_breadcrumbs,
                                    permissions,
                                    sp_unique_id=sp_unique_id,
                                )

                                await self._resolve_unresolved_viewers(file_entity, graph_client)
                                self._track_entity_groups(file_entity, site_url)

                                if files:
                                    pending_files.append(
                                        PendingFileDownload(
                                            entity=file_entity,
                                            drive_id=drive_id,
                                            item_id=item_data["id"],
                                        )
                                    )

                                    if len(pending_files) >= ITEM_BATCH_SIZE:
                                        downloaded = await self._download_files_parallel(
                                            pending_files, files
                                        )
                                        for ent in downloaded:
                                            yield ent
                                            entity_count += 1
                                        pending_files = []
                                else:
                                    yield file_entity
                                    entity_count += 1

                            except EntityProcessingError as e:
                                self.logger.warning(f"Skipping file: {e}")
                            except Exception as e:
                                self.logger.warning(f"Unexpected error processing file: {e}")

                    if pending_files and files:
                        downloaded = await self._download_files_parallel(pending_files, files)
                        for ent in downloaded:
                            yield ent
                            entity_count += 1

                    if cursor:
                        try:
                            _, delta_token = await graph_client.get_drive_delta(
                                drive_id, prefer_headers=self._delta_prefer_headers
                            )
                            if delta_token:
                                cursor_schema = SharePointOnlineCursor(**cursor.data)
                                cursor_schema.update_entity_cursor(
                                    drive_id=drive_id,
                                    delta_token=delta_token,
                                    changes_count=entity_count,
                                    is_full_sync=True,
                                )
                                cursor.update(**cursor_schema.model_dump())
                        except SourceAuthError:
                            raise
                        except Exception as e:
                            self.logger.warning(
                                f"Could not get delta token for drive {drive_id}: {e}"
                            )

                except EntityProcessingError as e:
                    self.logger.warning(f"Skipping drive {drive_id}: {e}")
                    continue

            if self._include_pages:
                try:
                    async for page_data in graph_client.get_pages(site_id):
                        try:
                            page_entity = await build_page_entity(
                                page_data, site_id, site_breadcrumbs, access=site_access
                            )
                            self._track_entity_groups(page_entity, site_url)
                            yield page_entity
                            entity_count += 1
                        except EntityProcessingError as e:
                            self.logger.warning(f"Skipping page: {e}")
                except SourceAuthError:
                    raise
                except Exception as e:
                    self.logger.debug(f"Pages not available for site {site_id}: {e}")

            if cursor:
                cursor_data = cursor.data
                synced_sites = cursor_data.get("synced_site_ids", {})
                synced_sites[site_id] = site_data.get("displayName", "")
                cursor.update(synced_site_ids=synced_sites)

        if cursor:
            cursor.update(
                full_sync_required=False,
                total_entities_synced=entity_count,
                tracked_entra_groups=list(self._item_level_entra_groups),
                tracked_sp_groups={
                    site: sorted(names) for site, names in self._item_level_sp_groups.items()
                },
            )

        self.logger.info(f"Full sync complete: {entity_count} entities")

    async def _incremental_sync(  # noqa: C901
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        *,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        cursor_data = cursor.data if cursor else {}
        schema = SharePointOnlineCursor(**cursor_data)
        delta_tokens = schema.drive_delta_tokens

        if not delta_tokens:
            self.logger.warning("No delta tokens for incremental sync, falling back to full")
            async for entity in self._full_sync(cursor, files):
                yield entity
            return

        changes_processed = 0
        graph_client = self._create_graph_client()
        label_filter = self._get_label_filter()

        for drive_id, token in delta_tokens.items():
            try:
                changed_items, new_token = await graph_client.get_drive_delta(
                    drive_id, token, prefer_headers=self._delta_prefer_headers
                )
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(f"Delta query failed for drive {drive_id}: {e}")
                if cursor:
                    cursor.update(full_sync_required=True)
                return

            self.logger.info(f"Drive {drive_id}: {len(changed_items)} changes")

            for item_data in changed_items:
                item_id = item_data.get("id", "")

                if item_data.get("deleted"):
                    spo_entity_id = f"spo:file:{drive_id}:{item_id}"
                    yield SharePointOnlineFileDeletionEntity(
                        drive_id=drive_id,
                        item_id=item_id,
                        spo_entity_id=spo_entity_id,
                        label=f"Deleted item {item_id} from drive {drive_id}",
                        deletion_status="removed",
                        breadcrumbs=[],
                    )
                    changes_processed += 1
                    continue

                if item_data.get("folder"):
                    continue

                if item_data.get("file"):
                    if node_selections and not await self._item_is_selected(
                        graph_client,
                        drive_id,
                        item_data,
                        node_selections,
                    ):
                        # A move out of a selected folder has to remove the old indexed
                        # copy. For a file that was always outside, this deletion matches
                        # no source object and is a harmless no-op.
                        yield self._deletion_entity(drive_id, item_id, "outside selected scope")
                        changes_processed += 1
                        continue
                    if label_filter is not None and await label_filter.should_skip_item(
                        drive_id=drive_id,
                        item_id=item_id,
                        item_name=item_data.get("name", ""),
                    ):
                        continue
                    try:
                        permissions = await graph_client.get_item_permissions(drive_id, item_id)
                        sp_unique_id = None
                        if self._has_link_permission(permissions):
                            sp_unique_id = await graph_client.get_item_sp_unique_id(
                                drive_id, item_id
                            )
                        file_entity = await build_file_entity(
                            item_data,
                            drive_id,
                            "",
                            [],
                            permissions,
                            sp_unique_id=sp_unique_id,
                        )
                        await self._resolve_unresolved_viewers(file_entity, graph_client)
                        self._track_entity_groups(file_entity)

                        if files:
                            file_entity = await self._download_and_save_file(
                                file_entity,
                                files,
                                drive_id,
                                item_id,
                            )
                        yield file_entity
                        changes_processed += 1
                    except (FileSkippedException, EntityProcessingError) as e:
                        self.logger.warning(f"Skipping changed file: {e}")

            if cursor and new_token:
                cursor_schema = SharePointOnlineCursor(**cursor.data)
                cursor_schema.update_entity_cursor(
                    drive_id=drive_id,
                    delta_token=new_token,
                    changes_count=changes_processed,
                )
                cursor.update(**cursor_schema.model_dump())

        self.logger.info(f"Incremental sync complete: {changes_processed} changes processed")

    @staticmethod
    def _deletion_entity(
        drive_id: str, item_id: str, reason: str
    ) -> SharePointOnlineFileDeletionEntity:
        return SharePointOnlineFileDeletionEntity(
            drive_id=drive_id,
            item_id=item_id,
            spo_entity_id=f"spo:file:{drive_id}:{item_id}",
            label=f"Removed item {item_id} from drive {drive_id} ({reason})",
            deletion_status="removed",
            breadcrumbs=[],
        )

    async def _item_is_selected(
        self,
        graph_client: GraphClient,
        drive_id: str,
        item_data: Dict[str, Any],
        selections: list[NodeSelectionData],
    ) -> bool:
        """Whether a delta item remains under one of the configured subtree roots."""
        selected_folders: set[str] = set()
        selected_files: set[str] = set()
        selected_sites = False
        whole_drive = False
        for selection in selections:
            metadata = selection.node_metadata or {}
            parsed = self._parse_browse_node_id(selection.source_node_id)
            selected_drive = str(
                metadata.get("drive_id") or parsed.get("drive_id") or ""
            )
            if selection.node_type == "site":
                selected_sites = True
            elif selected_drive != drive_id:
                continue
            elif selection.node_type == "drive":
                whole_drive = True
            elif selection.node_type == "folder":
                selected_folders.add(
                    str(
                        metadata.get("folder_id")
                        or parsed.get("node_id")
                        or ""
                    )
                )
            elif selection.node_type == "file":
                selected_files.add(
                    str(
                        metadata.get("item_id")
                        or parsed.get("node_id")
                        or ""
                    )
                )
        item_id = str(item_data.get("id") or "")
        if selected_sites or whole_drive or item_id in selected_files:
            return True
        if not selected_folders:
            return False

        parent_id = str((item_data.get("parentReference") or {}).get("id") or "")
        visited: set[str] = set()
        while parent_id and parent_id not in visited:
            if parent_id in selected_folders:
                return True
            visited.add(parent_id)
            try:
                parent = await graph_client.get(
                    f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}"
                )
            except SourceAuthError:
                raise
            except Exception as exc:
                # Unknown scope must fail closed. Treating it as selected could publish a
                # file from another matter folder; the reconciliation scan can restore it.
                self.logger.warning(
                    f"Could not resolve delta ancestry for {item_id}: {exc}; "
                    "treating it as outside the selected scope"
                )
                return False
            parent_id = str((parent.get("parentReference") or {}).get("id") or "")
        return False

    # -- Targeted Sync --

    async def _targeted_sync(  # noqa: C901
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        node_selections: list[NodeSelectionData],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Sync only the nodes specified in node_selections."""
        entity_count = 0

        site_ids: set = set()
        drive_selections: List[NodeSelectionData] = []
        synced_drive_ids: set[str] = set()

        for sel in node_selections:
            if sel.node_type == "site":
                site_ids.add(sel.node_metadata.get("site_id", "") if sel.node_metadata else "")
            elif sel.node_type in ("drive", "folder", "file"):
                drive_selections.append(sel)
                if sel.node_metadata and sel.node_metadata.get("site_id"):
                    site_ids.add(sel.node_metadata["site_id"])

        graph_client = self._create_graph_client()

        for site_id in site_ids:
            if not site_id:
                continue

            has_specific_drives = any(
                s.node_metadata
                and s.node_metadata.get("site_id") == site_id
                and s.node_type in ("drive", "folder", "file")
                for s in drive_selections
            )
            if has_specific_drives:
                continue

            try:
                site_data = await graph_client.get_site(site_id)
                targeted_site_url = self._normalize_site_url(site_data.get("webUrl", ""))

                # Fetch site-level permissions from first drive root
                targeted_site_access = None
                async for peek_drive in graph_client.get_drives(site_id):
                    try:
                        perms = await graph_client.get_drive_root_permissions(peek_drive["id"])
                        targeted_site_access = await extract_access_control(perms)
                    except Exception:
                        pass
                    break

                site_entity = await build_site_entity(site_data, [], access=targeted_site_access)
                self._track_entity_groups(site_entity, targeted_site_url)
                yield site_entity
                entity_count += 1
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(f"Targeted sync: skipping site {site_id}: {e}")
                continue

            site_breadcrumbs = [
                Breadcrumb(
                    entity_id=site_entity.site_id,
                    name=site_entity.display_name,
                    entity_type="SharePointOnlineSiteEntity",
                )
            ]

            async for drive_data in graph_client.get_drives(site_id):
                drive_id = drive_data.get("id", "")
                async for ent in self._sync_drive(
                    graph_client, drive_id, site_id, site_breadcrumbs, files
                ):
                    yield ent
                    entity_count += 1
                if drive_id:
                    synced_drive_ids.add(drive_id)

        for sel in drive_selections:
            meta = sel.node_metadata or {}
            # The node id already encodes the ids ("folder:{drive}|{node}"), so a selection
            # whose metadata was lost in transit still resolves. Metadata stays first: it is
            # what a caller can enrich, the id is only ever the fallback.
            parsed = self._parse_browse_node_id(sel.source_node_id)

            if sel.node_type == "drive":
                drive_id = meta.get("drive_id", "")
                sel_site_id = meta.get("site_id", "")
                if not drive_id:
                    self.logger.error(
                        f"Targeted sync: drive selection {sel.source_node_id!r} carries no "
                        "drive_id and was skipped. A scoped connection that cannot resolve "
                        "its roots syncs nothing; re-select the folders in the admin UI."
                    )
                    continue
                async for ent in self._sync_drive(graph_client, drive_id, sel_site_id, [], files):
                    yield ent
                    entity_count += 1
                synced_drive_ids.add(drive_id)

            elif sel.node_type == "folder":
                drive_id = meta.get("drive_id", "") or parsed.get("drive_id", "")
                folder_id = meta.get("folder_id", "") or parsed.get("node_id", "")
                if not drive_id or not folder_id:
                    self.logger.error(
                        f"Targeted sync: folder selection {sel.source_node_id!r} carries no "
                        "drive_id/folder_id and was skipped. A scoped connection that cannot "
                        "resolve its roots syncs nothing; re-select the folders in the admin UI."
                    )
                    continue
                try:
                    async for ent in self._sync_folder_recursive(
                        graph_client, drive_id, folder_id, "", files
                    ):
                        yield ent
                        entity_count += 1
                    synced_drive_ids.add(drive_id)
                except SourceAuthError:
                    raise
                except Exception as e:
                    # A folder that was deleted or unshared costs the firm that one root,
                    # not the run — and never widens the sync back to the whole library.
                    self.logger.warning(
                        f"Targeted sync: skipping folder {folder_id} ({e}); "
                        "the remaining selected folders still sync"
                    )

            elif sel.node_type == "file":
                drive_id = meta.get("drive_id", "") or parsed.get("drive_id", "")
                item_id = meta.get("item_id", "") or parsed.get("node_id", "")
                if not drive_id or not item_id:
                    self.logger.error(
                        f"Targeted sync: file selection {sel.source_node_id!r} carries no "
                        "drive_id/item_id and was skipped."
                    )
                    continue
                try:
                    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                    item_data = await graph_client.get(url)
                    if item_data.get("file"):
                        permissions = await graph_client.get_item_permissions(drive_id, item_id)
                        sp_unique_id = None
                        if self._has_link_permission(permissions):
                            sp_unique_id = await graph_client.get_item_sp_unique_id(
                                drive_id, item_id
                            )
                        file_entity = await build_file_entity(
                            item_data,
                            drive_id,
                            "",
                            [],
                            permissions,
                            sp_unique_id=sp_unique_id,
                        )
                        await self._resolve_unresolved_viewers(file_entity, graph_client)
                        self._track_entity_groups(file_entity)
                        if files:
                            file_entity = await self._download_and_save_file(
                                file_entity, files, drive_id, item_id
                            )
                        yield file_entity
                        entity_count += 1
                        synced_drive_ids.add(drive_id)
                except SourceAuthError:
                    raise
                except Exception as e:
                    self.logger.warning(f"Targeted sync: skipping file {item_id}: {e}")

        # Seed one delta cursor for every document library covered by this targeted full
        # scan. Subsequent provider events and scheduled reconciliations can now fetch
        # only changed driveItems; previously every scoped SharePoint wakeup re-enumerated
        # and downloaded the entire selected folder.
        if cursor:
            for drive_id in sorted(synced_drive_ids):
                try:
                    _, delta_token = await graph_client.get_drive_delta(
                        drive_id, prefer_headers=self._delta_prefer_headers
                    )
                    if not delta_token:
                        continue
                    schema = SharePointOnlineCursor(**cursor.data)
                    schema.update_entity_cursor(
                        drive_id=drive_id,
                        delta_token=delta_token,
                        changes_count=entity_count,
                        is_full_sync=True,
                    )
                    cursor.update(**schema.model_dump())
                except SourceAuthError:
                    raise
                except Exception as exc:
                    self.logger.warning(
                        f"Could not seed targeted delta token for drive {drive_id}: {exc}"
                    )

        # A scoped run that yields nothing is indistinguishable from a healthy empty drive
        # in the sync report, and the tombstone guard reads it as "the source is gone".
        # Selections were configured, so zero entities means they failed to resolve — say so.
        if entity_count == 0 and node_selections:
            self.logger.error(
                f"Targeted sync resolved {len(node_selections)} selected root(s) to zero "
                "entities. The selection is unusable — nothing was indexed. Re-select the "
                "folders in the admin UI so the connection stores their ids again."
            )
        self.logger.info(f"Targeted sync complete: {entity_count} entities")

    async def _sync_drive(
        self,
        graph_client: GraphClient,
        drive_id: str,
        site_id: str,
        site_breadcrumbs: List[Breadcrumb],
        files: FileService | None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Sync all files in a single drive (used by both full and targeted sync)."""
        try:
            drive_data = await graph_client.get_drive(drive_id)

            # Fetch drive root permissions for the drive entity
            drive_access = None
            try:
                drive_permissions = await graph_client.get_drive_root_permissions(drive_id)
                drive_access = await extract_access_control(drive_permissions)
            except Exception:
                pass

            drive_entity = await build_drive_entity(
                drive_data, site_id, site_breadcrumbs, access=drive_access
            )
            self._track_entity_groups(drive_entity)
            yield drive_entity

            drive_breadcrumbs = site_breadcrumbs + [
                Breadcrumb(
                    entity_id=drive_entity.drive_id,
                    name=drive_entity.name,
                    entity_type="SharePointOnlineDriveEntity",
                )
            ]

            item_stream = graph_client.get_drive_items_recursive(drive_id)
            async for entity in self._process_file_items(
                graph_client, item_stream, drive_id, site_id, drive_breadcrumbs, files
            ):
                yield entity
        except EntityProcessingError as e:
            self.logger.warning(f"Skipping drive {drive_id}: {e}")

    async def _sync_folder_recursive(
        self,
        graph_client: GraphClient,
        drive_id: str,
        folder_id: str,
        site_id: str,
        files: FileService | None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Recursively sync all files under a specific folder."""
        item_stream = graph_client.get_drive_items_recursive(drive_id, folder_id)
        async for entity in self._process_file_items(
            graph_client,
            item_stream,
            drive_id,
            site_id,
            [],
            files,
            resolve_viewers=True,
        ):
            yield entity

    async def _process_file_items(  # noqa: C901
        self,
        graph_client: GraphClient,
        item_stream: AsyncGenerator[Dict[str, Any], None],
        drive_id: str,
        site_id: str,
        breadcrumbs: List[Breadcrumb],
        files: FileService | None,
        *,
        resolve_viewers: bool = False,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Iterate drive items, build file entities, and yield with batched downloads."""
        pending_files: List[PendingFileDownload] = []
        label_filter = self._get_label_filter()

        async for item_data in item_stream:
            if item_data.get("folder") or not item_data.get("file"):
                continue
            if label_filter is not None and await label_filter.should_skip_item(
                drive_id=drive_id,
                item_id=item_data["id"],
                item_name=item_data.get("name", ""),
            ):
                continue
            try:
                permissions = await graph_client.get_item_permissions(drive_id, item_data["id"])
                sp_unique_id = None
                if self._has_link_permission(permissions):
                    sp_unique_id = await graph_client.get_item_sp_unique_id(
                        drive_id, item_data["id"]
                    )
                file_entity = await build_file_entity(
                    item_data,
                    drive_id,
                    site_id,
                    breadcrumbs,
                    permissions,
                    sp_unique_id=sp_unique_id,
                )
                if resolve_viewers:
                    await self._resolve_unresolved_viewers(file_entity, graph_client)
                self._track_entity_groups(file_entity)

                if files:
                    pending_files.append(
                        PendingFileDownload(
                            entity=file_entity,
                            drive_id=drive_id,
                            item_id=item_data["id"],
                        )
                    )
                    if len(pending_files) >= ITEM_BATCH_SIZE:
                        downloaded = await self._download_files_parallel(pending_files, files)
                        for ent in downloaded:
                            yield ent
                        pending_files = []
                else:
                    yield file_entity
            except EntityProcessingError as e:
                self.logger.warning(f"Skipping file: {e}")

        if pending_files and files:
            downloaded = await self._download_files_parallel(pending_files, files)
            for ent in downloaded:
                yield ent

    # -- Validation --

    async def validate(self) -> None:
        """Validate credentials by pinging the root site endpoint."""
        await self._get(f"{GRAPH_BASE_URL}/sites/root")

    # -- Access Control Memberships --

    async def _expand_entra_groups(
        self, group_expander: EntraGroupExpander
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand tracked Entra ID groups into user memberships."""
        entra_group_ids = list(self._item_level_entra_groups)
        self.logger.info(f"Expanding {len(entra_group_ids)} Entra ID groups")
        for group_ref in entra_group_ids:
            group_id = group_ref.split(":", 1)[1] if ":" in group_ref else group_ref
            async for membership in group_expander.expand_group(group_id):
                yield membership

    async def _expand_sp_site_groups(  # noqa: C901
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand tracked SP site groups into user/group memberships.

        Iterates per-site: for each site URL we've tracked SP group names against,
        fetches that site's SP groups via the SharePoint REST API and resolves
        their members.

        Member types emitted:
        - ``user`` for real users (PrincipalType=1). Role principals like
          "Everyone except external users" are skipped.
        - ``group`` for Entra security groups nested inside SP groups
          (PrincipalType=4 with federateddirectoryclaimprovider). The broker's
          recursive group expansion resolves these to individual users at
          search time.
        """
        if not self._item_level_sp_groups:
            return

        total_groups = sum(len(v) for v in self._item_level_sp_groups.values())
        self.logger.info(
            f"Expanding {total_groups} SP site groups across "
            f"{len(self._item_level_sp_groups)} site(s)"
        )

        graph_client = self._create_graph_client()

        for site_url, sp_group_names in self._item_level_sp_groups.items():
            if not site_url or not sp_group_names:
                continue

            sp_token_provider = self._make_sp_token_provider_for_site(site_url)
            if not sp_token_provider:
                self.logger.warning(
                    f"No SP token provider for site {site_url}; skipping SP group expansion"
                )
                continue

            try:
                sp_groups = await graph_client.get_site_groups(
                    site_url, sp_token_provider=sp_token_provider
                )
            except Exception as e:
                self.logger.warning(f"Failed to fetch SP groups for {site_url}: {e}")
                continue

            sp_name_to_id = {
                f"sp:{g['Title'].replace(' ', '_').lower()}": g.get("Id")
                for g in sp_groups
                if g.get("Title")
            }

            for sp_name in sp_group_names:
                sp_id = sp_name_to_id.get(sp_name)
                if not sp_id:
                    self.logger.debug(f"SP group '{sp_name}' not found in site {site_url}")
                    continue

                try:
                    users = await graph_client.get_site_group_users(
                        site_url, sp_id, sp_token_provider=sp_token_provider
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Failed to fetch users for SP group {sp_name} in {site_url}: {e}"
                    )
                    continue

                for user in users:
                    parsed = self._parse_sp_group_member(user)
                    if parsed is None:
                        if self._is_unrecognized_pt4_login(user):
                            self.logger.info(
                                "Unrecognized PrincipalType=4 SP group member; skipped. "
                                f"LoginName={user.get('LoginName', '')!r} "
                                f"Title={user.get('Title', '')!r}"
                            )
                        continue
                    member_id, member_type = parsed
                    yield MembershipTuple(
                        member_id=member_id,
                        member_type=member_type,
                        group_id=sp_name,
                        group_name=user.get("Title") or sp_name,
                    )
                    if member_id == EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL:
                        self._needs_internal_user_enum = True

    async def _expand_everyone_except_external(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Populate the synthetic ``EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL`` group.

        Called once per sync, only when the SP group expansion observed at
        least one occurrence of the claim. Enumerates internal tenant users
        via Graph (``userType eq 'Member'`` filter excludes B2B guests) and
        yields one user → claim membership per user. The broker's recursive
        group expansion then chains user → claim → SP group at search time.
        """
        graph_client = self._create_graph_client()
        count = 0
        try:
            async for u in graph_client.list_internal_tenant_users():
                count += 1
                yield MembershipTuple(
                    member_id=u["email"],
                    member_type="user",
                    group_id=EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL,
                    group_name=EVERYONE_EXCEPT_EXTERNAL_DISPLAY_NAME,
                )
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Failed to enumerate internal tenant users for "
                f"'{EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL}': {e}"
            )
        self.logger.info(
            f"Populated synthetic '{EVERYONE_EXCEPT_EXTERNAL_PRINCIPAL}' group "
            f"with {count} internal tenant users"
        )

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand Entra ID groups and SP site groups into user memberships."""
        self.logger.info("Starting access control membership extraction")
        membership_count = 0
        self._needs_internal_user_enum = False
        group_expander = self._create_group_expander()

        async for m in self._expand_entra_groups(group_expander):
            yield m
            membership_count += 1

        try:
            async for m in self._expand_sp_site_groups():
                yield m
                membership_count += 1
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"SP site group expansion failed: {e}")

        # If any SP site group contained the "Everyone except external users"
        # claim, populate the synthetic claim group with internal tenant users
        # exactly once. Skipped entirely when no group used the claim.
        if self._needs_internal_user_enum:
            async for m in self._expand_everyone_except_external():
                yield m
                membership_count += 1

        group_expander.log_stats()
        self.logger.info(f"Access control extraction complete: {membership_count} memberships")


# =============================================================================
# OAuth source — delegated user auth
# =============================================================================


@source(
    name="SharePoint Online",
    short_name="sharepoint_online",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_ROTATING_REFRESH,
    auth_config_class=None,
    config_class=SharePointOnlineConfig,
    supports_continuous=True,
    cursor_class=SharePointOnlineCursor,
    supports_access_control=True,
    supports_browse_tree=True,
    feature_flag="sharepoint_2019_v2",
    labels=["Collaboration", "File Storage"],
)
class SharePointOnlineSource(SharePointOnlineBase):
    """SharePoint Online source using delegated OAuth.

    Uses the signed-in user's permissions via OAuth browser flow.
    Site discovery uses Graph search (delegated permissions).
    """

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: SharePointOnlineConfig,
    ) -> SharePointOnlineSource:
        """Create and configure an OAuth SharePoint Online source."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._init_common(config)
        return instance

    async def _get_access_token(self) -> str:
        return await self.auth.get_token()

    async def _handle_401(self) -> str:
        if self.auth.supports_refresh:
            return await self.auth.force_refresh()
        return await self.auth.get_token()

    def _make_sp_token_provider_for_site(self, site_url: str) -> Optional[Callable]:
        """Create SP token provider for a specific site URL via OAuth scope exchange."""
        if not site_url:
            return None
        parsed = urlparse(site_url)
        hostname = parsed.netloc
        if not hostname:
            return None
        sp_scope = f"https://{hostname}/.default"

        async def _provider() -> str:
            token = await self.get_token_for_resource(sp_scope)
            if not token:
                raise RuntimeError(f"Could not obtain SharePoint token for scope {sp_scope}")
            return token

        return _provider

    async def _discover_sites(self, graph_client: GraphClient) -> List[Dict[str, Any]]:
        """Discover sites via Graph search (delegated permissions).

        Supports:
          - Single URL: "https://tenant.sharepoint.com/sites/MySite"
          - Comma-separated: "https://tenant.sharepoint.com/sites/A, .../sites/B"
          - Empty string: search all accessible sites
        """
        sites = []

        if self._site_url:
            urls = [u.strip() for u in self._site_url.split(",") if u.strip()]
            for url in urls:
                parsed = urlparse(url)
                hostname = parsed.netloc
                site_path = parsed.path.lstrip("/")
                try:
                    site = await graph_client.get_site_by_url(hostname, site_path)
                    sites.append(site)
                except SourceAuthError:
                    raise
                except Exception as e:
                    self.logger.warning(f"Could not resolve site URL {url}: {e}")
                    raise
        else:
            # `/sites?search=*` is the richest enumeration but the least reliable: it is
            # backed by the search index, which is empty for hours after a tenant is
            # provisioned and returns 500 while it builds. Falling back keeps a new tenant
            # usable instead of presenting an operator with an opaque provider error on a
            # connection that is, in fact, perfectly healthy.
            try:
                async for site in graph_client.search_sites("*"):
                    if not self._include_personal_sites and site.get("isPersonalSite", False):
                        continue
                    sites.append(site)
            except SourceAuthError:
                raise
            except Exception as exc:
                self.logger.warning(
                    f"Site search unavailable ({exc}); falling back to the root site. "
                    "Set a site URL on the connection to target sites directly."
                )
            if not sites:
                try:
                    sites.append(await graph_client.get_site("root"))
                except SourceAuthError:
                    raise
                except Exception as exc:
                    self.logger.error(
                        f"Could not enumerate any SharePoint site ({exc}). The grant may not "
                        "reach a site, or SharePoint is not licensed on this tenant."
                    )

        self.logger.info(f"Discovered {len(sites)} sites to sync")
        return sites
