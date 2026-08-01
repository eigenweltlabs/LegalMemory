"""OneDrive source implementation using Microsoft Graph API.

Retrieves data from a user's OneDrive, including:
 - Drive information (OneDriveDriveEntity objects)
 - DriveItems (OneDriveDriveItemEntity objects) for files and folders

This handles different OneDrive scenarios:
 - Personal OneDrive (with SPO license)
 - OneDrive without SPO license (app folder only)
 - Business OneDrive

Incremental sync:
 - Uses Graph delta queries (/drives/{id}/root/delta), the same change model as
   SharePoint document libraries
 - Per-drive delta tokens stored in the cursor
 - The token for the next incremental run is minted *before* the full crawl, so
   changes made while the crawl is running are replayed by the first delta drain
   instead of being lost until the next periodic full scan

Access graph generation:
 - Mirrors per-item permissions from Graph (grantedToV2 identity sets)
 - Expands Entra ID groups referenced by those permissions via /groups/{id}/members
   so a group-shared file is retrievable by the group's members, not only by
   principals named directly

Reference (Microsoft Graph API):
  https://learn.microsoft.com/en-us/graph/api/drive-get?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0
  https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0
"""

from collections import deque
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.types import MembershipTuple, RateLimitLevel
from knowledge_index.connectors.runtime.types import BrowseNode, NodeSelectionData
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.cursors.onedrive import OneDriveCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.acl import graph_permissions_to_access
from knowledge_index.connectors.configs import OneDriveConfig
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity
from knowledge_index.connectors.entities.onedrive import (
    OneDriveDriveEntity,
    OneDriveDriveItemDeletionEntity,
    OneDriveDriveItemEntity,
)
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.sources.microsoft_sensitivity_labels import SensitivityLabelFilter
from knowledge_index.connectors.sources.sharepoint_online.graph_groups import EntraGroupExpander
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.types import AuthenticationMethod, OAuthType

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Hosts the firm's Microsoft token may be sent to. Graph returns pre-authenticated
# download URLs on *.sharepoint.com and *.files.1drv.com; those carry their own
# token in the query string and must not also receive the firm's.
GRAPH_AUTH_HOSTS = ("graph.microsoft.com",)

# The virtual drive id used when only app-folder access is available. It is not a real
# Graph drive, so it has no delta feed and no per-item permission mirror worth trusting.
APP_FOLDER_DRIVE_ID = "appfolder"

# Same Prefer headers SharePoint uses on the driveItem delta. Without them an event can
# wake the delta feed for a permission revocation and Graph would return no item,
# leaving access alive until the periodic full scan.
DELTA_PREFER_HEADERS = (
    "deltashowsharingchanges",
    "deltashowremovedasdeleted",
    "deltatraversepermissiongaps",
)


@source(
    name="OneDrive",
    short_name="onedrive",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    auth_config_class=None,
    config_class=OneDriveConfig,
    labels=["File Storage"],
    supports_continuous=True,
    cursor_class=OneDriveCursor,
    supports_access_control=True,
    supports_browse_tree=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class OneDriveSource(BaseSource):
    """OneDrive source connector integrates with the Microsoft Graph API to extract files.

    Supports OneDrive personal and business accounts.

    It supports various OneDrive scenarios including
    personal drives, business drives, and app folder access with intelligent fallback handling.
    """

    _excluded_sensitivity_label_ids: List[str]
    _skip_encrypted_files: bool
    _skip_unlabeled_files: bool
    _mirror_permissions: bool
    _label_filter: Optional[SensitivityLabelFilter]
    # Entra group ids ("entra:<guid>") seen in mirrored item ACLs during this run.
    # Expanded into memberships afterwards; also persisted in the cursor so a later
    # run remembers which groups keep group-shared files reachable.
    _tracked_entra_groups: set
    # Cache of Entra user object id -> email for this run. Graph returns id-only
    # identity sets for app-created shares; one lookup per person, not per item.
    _user_id_emails: dict

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: OneDriveConfig,
    ) -> "OneDriveSource":
        """Create a new OneDrive source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._excluded_sensitivity_label_ids = list(config.excluded_sensitivity_label_ids)
        instance._skip_encrypted_files = config.skip_encrypted_files
        instance._skip_unlabeled_files = config.skip_unlabeled_files
        instance._mirror_permissions = config.mirror_permissions
        instance._label_filter = None
        instance._tracked_entra_groups = set()
        instance._user_id_emails = {}
        return instance

    def _get_label_filter(self) -> Optional[SensitivityLabelFilter]:
        """Lazily build a Purview sensitivity-label filter from config."""
        if self._label_filter is not None:
            return self._label_filter
        if not self._excluded_sensitivity_label_ids and not self._skip_unlabeled_files:
            return None
        self._label_filter = SensitivityLabelFilter(
            excluded_label_ids=self._excluded_sensitivity_label_ids,
            skip_encrypted=self._skip_encrypted_files,
            skip_unlabeled=self._skip_unlabeled_files,
            http_client=self.http_client,
            token_provider=self.get_access_token,
            logger=self.logger,
        )
        return self._label_filter

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(
        self,
        url: str,
        params: Optional[Dict] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """Make an authenticated GET request to Microsoft Graph API with retry logic."""
        token = await self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            self.logger.warning(
                f"Got 401 Unauthorized from Microsoft Graph API at {url}, refreshing token..."
            )
            new_token = await self.auth.force_refresh()
            headers["Authorization"] = f"Bearer {new_token}"
            response = await self.http_client.get(url, headers=headers, params=params)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    async def _get_available_drives(self) -> List[Dict]:
        """Get all available drives for the user.

        This endpoint works better for accounts without SPO license.
        """
        try:
            url = f"{GRAPH_BASE_URL}/me/drives"
            data = await self._get(url)
            return data.get("value", [])
        except SourceAuthError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                self.logger.warning("Cannot access /me/drives, will try app folder access")
                return []
            raise

    async def _get_user_drive(self) -> Optional[Dict]:
        """Get the user's default OneDrive with fallback handling.

        Tries multiple approaches based on available permissions.
        """
        try:
            url = f"{GRAPH_BASE_URL}/me/drive"
            return await self._get(url)
        except SourceAuthError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                error_body = e.response.json() if hasattr(e.response, "json") else {}
                if "SPO license" in str(error_body):
                    self.logger.warning(
                        "Tenant does not have SPO license, trying alternative endpoints"
                    )
                    drives = await self._get_available_drives()
                    if drives:
                        self.logger.debug(f"Found {len(drives)} drives via /me/drives")
                        return drives[0]
                    else:
                        self.logger.debug("No drives found, will create virtual app folder drive")
                        return None
            raise

    async def _create_app_folder_drive(self) -> Dict:
        """Create a virtual drive object for app folder access.

        When full OneDrive access isn't available, we can still access app-specific folders.
        """
        return {
            "id": APP_FOLDER_DRIVE_ID,
            "name": "OneDrive App Folder",
            "driveType": "personal",
            "owner": {"user": {"displayName": "Current User"}},
            "quota": None,
            "createdDateTime": None,
            "lastModifiedDateTime": None,
        }

    async def _generate_drive_entity(self) -> AsyncGenerator[OneDriveDriveEntity, None]:
        """Generate OneDriveDriveEntity for the user's drive(s)."""
        drive_obj = await self._get_user_drive()

        if not drive_obj:
            drive_obj = await self._create_app_folder_drive()
            self.logger.debug("Using app folder access mode")

        self.logger.debug(f"Drive: {drive_obj}")

        drive_name = drive_obj.get("name") or drive_obj.get("driveType", "OneDrive")

        yield OneDriveDriveEntity(
            breadcrumbs=[],
            id=drive_obj["id"],
            name=drive_name,
            created_at=drive_obj.get("createdDateTime"),
            updated_at=drive_obj.get("lastModifiedDateTime"),
            drive_type=drive_obj.get("driveType"),
            owner=drive_obj.get("owner"),
            quota=drive_obj.get("quota"),
            web_url_override=drive_obj.get("webUrl"),
        )

    async def _list_drive_items(
        self,
        drive_id: str,
        folder_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """List items in a drive using pagination.

        Args:
            drive_id: ID of the drive
            folder_id: ID of specific folder, or None for root
        """
        if drive_id == APP_FOLDER_DRIVE_ID:
            url = f"{GRAPH_BASE_URL}/me/drive/special/approot/children"
        elif folder_id:
            url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{folder_id}/children"
        else:
            url = f"{GRAPH_BASE_URL}/drives/{drive_id}/root/children"

        params = {
            "$top": 100,
            "$select": (
                "id,name,size,createdDateTime,lastModifiedDateTime,"
                "file,folder,parentReference,webUrl"
            ),
        }

        try:
            while url:
                data = await self._get(url, params=params)

                for item in data.get("value", []):
                    self.logger.debug(f"DriveItem: {item}")
                    yield item

                url = data.get("@odata.nextLink")
                if url:
                    params = None
        except SourceAuthError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                self.logger.warning(f"Access denied to folder {folder_id}, skipping")
                return
            elif e.response.status_code == 404:
                self.logger.warning(f"Folder {folder_id} not found, skipping")
                return
            else:
                raise

    # ------------------------------------------------------------------ access control

    def _track_access_groups(self, access: Optional[AccessControl]) -> None:
        """Remember every Entra group a mirrored ACL grants read to.

        The grant alone is not usable: nobody authenticates to this appliance as a
        directory GUID. The tracked set is expanded into user memberships after the
        scan and persisted in the cursor so incremental runs keep it.
        """
        if access is None:
            return
        for viewer in access.viewers or []:
            if viewer.startswith("group:entra:"):
                self._tracked_entra_groups.add(viewer[len("group:") :])

    async def _get_item_access(self, drive_id: str, item_id: str) -> Optional[AccessControl]:
        """Read a DriveItem's permissions so the file is retrievable by whoever can open it.

        Returns None when permissions could not be read at all. None means "unknown" and
        keeps the item fail-closed; returning an empty AccessControl instead would assert
        that nobody may read it, which is a different and wrong claim. A failure here is
        never allowed to abort the scan or drop the document.
        """
        if not self._mirror_permissions:
            return None
        url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/permissions"
        try:
            data = await self._get(url)
        except Exception as e:
            self.logger.warning(f"Could not read permissions for item {item_id}: {e}")
            return None
        access = graph_permissions_to_access(data.get("value"))
        await self._resolve_viewer_ids(access)
        self._track_access_groups(access)
        return access

    async def _resolve_viewer_ids(self, access: Optional[AccessControl]) -> None:
        """Resolve ``user:id:{guid}`` viewers to ``user:{email}`` in place.

        Graph returns id-only identity sets for shares created by applications, and
        nobody authenticates to this appliance as a directory GUID — leaving the id
        unresolved would keep the grant mirrored but forever unmatchable. An id that
        cannot be resolved is dropped, which is fail-closed for that one viewer.
        """
        if access is None:
            return
        unresolved = [v for v in access.viewers or [] if v.startswith("user:id:")]
        if not unresolved:
            return
        resolved: list[str] = []
        for viewer in access.viewers:
            if not viewer.startswith("user:id:"):
                resolved.append(viewer)
                continue
            user_id = viewer[len("user:id:") :]
            if user_id not in self._user_id_emails:
                try:
                    data = await self._get(
                        f"{GRAPH_BASE_URL}/users/{user_id}",
                        params={"$select": "mail,userPrincipalName"},
                    )
                    email = str(
                        data.get("mail") or data.get("userPrincipalName") or ""
                    ).strip().lower()
                    self._user_id_emails[user_id] = email if "@" in email else None
                except SourceAuthError:
                    raise
                except Exception as e:
                    self.logger.warning(f"Could not resolve user id {user_id}: {e}")
                    self._user_id_emails[user_id] = None
            email = self._user_id_emails[user_id]
            if email:
                resolved.append(f"user:{email}")
            else:
                self.logger.warning(f"Dropping unresolvable user viewer: {viewer}")
        access.viewers = sorted(set(resolved))

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand tracked Entra ID groups into user memberships.

        Without these rows a grant to ``group:entra:<guid>`` matches no caller and a
        group-shared file is invisible rather than protected. Groups come from the ACLs
        mirrored during this run plus the set remembered in the cursor.
        """
        if not self._mirror_permissions:
            return
        group_refs = sorted(self._tracked_entra_groups)
        if not group_refs:
            return
        self.logger.info(f"Expanding {len(group_refs)} Entra ID groups")
        expander = EntraGroupExpander(
            access_token_provider=self.get_access_token,
            http_client=self.http_client,
            logger=self.logger,
        )
        for group_ref in group_refs:
            group_id = group_ref.split(":", 1)[1] if ":" in group_ref else group_ref
            async for membership in expander.expand_group(group_id):
                yield membership
        expander.log_stats()

    # ------------------------------------------------------------------------ download

    def _get_download_url(self, drive_id: str, item_id: str) -> Optional[str]:
        """Get the download URL for a specific file item.

        Returns a Graph API content endpoint URL that can be used with the access token.
        """
        return f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/content"

    @staticmethod
    def _selected_folder_ids(
        node_selections: Optional[List[NodeSelectionData]],
    ) -> List[str]:
        """The drive-item ids of the subtrees this connection was scoped to."""
        folder_ids: List[str] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            folder_id = str(metadata.get("folder_id") or selection.source_node_id or "").strip()
            if folder_id and folder_id not in folder_ids:
                folder_ids.append(folder_id)
        return folder_ids

    async def _list_all_drive_items_recursively(
        self,
        drive_id: str,
        start_folder_ids: Optional[List[str]] = None,
    ) -> AsyncGenerator[Dict, None]:
        """Recursively list items in a drive using BFS approach.

        ``start_folder_ids`` seeds the walk at chosen folders instead of the drive root.
        Because it is still a BFS, a matter folder created after the scope was set is
        picked up on the next sync without anyone re-choosing it.
        """
        roots: List[Optional[str]] = list(start_folder_ids or [None])
        selected_roots = set(start_folder_ids or ())
        folder_queue = deque(roots)
        processed_folders = set()

        while folder_queue:
            current_folder_id = folder_queue.popleft()

            if current_folder_id in processed_folders:
                continue
            processed_folders.add(current_folder_id)

            try:
                async for item in self._list_drive_items(drive_id, current_folder_id):
                    yield item

                    if "folder" in item and len(folder_queue) < 100:
                        folder_queue.append(item["id"])
            except SourceAuthError:
                raise
            except Exception as e:
                if current_folder_id in selected_roots:
                    # Never widen the walk to compensate: an operator who scoped this
                    # connection to one matter folder must not get the whole drive
                    # because that folder was deleted or its grant withdrawn.
                    self.logger.warning(
                        f"Selected folder {current_folder_id} could not be read ({e}); "
                        "skipping it — the remaining selected folders still sync"
                    )
                else:
                    self.logger.warning(f"Error processing folder {current_folder_id}: {e}")
                continue

    async def _download_file_entity(
        self,
        file_entity: OneDriveDriveItemEntity,
        files: FileService,
    ) -> bool:
        """Stage one file's bytes. Returns True when the entity should be yielded."""
        try:
            await files.download_from_url(
                entity=file_entity,
                client=self.http_client,
                auth=self.auth,
                logger=self.logger,
                auth_hosts=GRAPH_AUTH_HOSTS,
            )
            if not file_entity.local_path:
                self.logger.warning(f"Download produced no local path for {file_entity.name}")
                return False
            return True
        except FileSkippedException as e:
            self.logger.debug(f"Skipping file {file_entity.name}: {e.reason}")
            return False
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            self.logger.warning(
                f"HTTP {e.response.status_code} downloading {file_entity.name}: {e}"
            )
            return False

    async def _generate_drive_item_entities(  # noqa: C901
        self,
        drive_id: str,
        drive_name: str,
        files: FileService | None = None,
        start_folder_ids: Optional[List[str]] = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate OneDriveDriveItemEntity objects for files in the drive."""
        file_count = 0
        label_filter = self._get_label_filter()
        async for item in self._list_all_drive_items_recursively(drive_id, start_folder_ids):
            if "folder" in item:
                continue

            # Run the label check outside the per-item try so that errors
            # configured to fail loud (skip_encrypted_files=False) propagate
            # instead of being swallowed by the broad exception handler below.
            if label_filter is not None and await label_filter.should_skip_item(
                drive_id=drive_id,
                item_id=item["id"],
                item_name=item.get("name", ""),
            ):
                continue

            try:
                download_url = self._get_download_url(drive_id, item["id"])

                file_entity = OneDriveDriveItemEntity.from_api(
                    item, drive_name=drive_name, drive_id=drive_id, download_url=download_url
                )

                if not file_entity:
                    continue

                file_entity.access = await self._get_item_access(drive_id, item["id"])

                if files:
                    if not await self._download_file_entity(file_entity, files):
                        continue
                    file_count += 1
                    self.logger.debug(f"Processed file {file_count}: {file_entity.name}")
                    yield file_entity
                else:
                    yield file_entity

            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(f"Failed to process item {item.get('name', 'unknown')}: {e}")
                continue

        self.logger.debug(f"Total files processed: {file_count}")

    # -------------------------------------------------------------------- delta / sync

    async def _get_drive_delta(
        self,
        drive_id: str,
        delta_token: str = "",
    ) -> Tuple[List[Dict], str]:
        """Drain the drive's delta feed. Returns (changed_items, new_delta_link)."""
        url = delta_token or f"{GRAPH_BASE_URL}/drives/{drive_id}/root/delta"
        prefer = {"Prefer": ", ".join(DELTA_PREFER_HEADERS)}
        items: List[Dict] = []
        delta_link = ""
        while url:
            data = await self._get(url, extra_headers=prefer)
            items.extend(data.get("value", []))
            delta_link = data.get("@odata.deltaLink", delta_link)
            url = data.get("@odata.nextLink")
        self.logger.info(
            f"Delta query for drive {drive_id}: {len(items)} items, "
            f"has_new_token={bool(delta_link)}"
        )
        return items, delta_link

    async def _get_latest_delta_token(self, drive_id: str) -> str:
        """Mint a delta link for the drive's *current* state without enumerating it.

        Called before a full crawl: everything that changes while the crawl runs is then
        replayed by the first incremental drain. Minting the token after the crawl would
        silently skip an item created mid-crawl in an already-visited folder until some
        later change touched it again.
        """
        items, delta_link = await self._get_drive_delta(drive_id, delta_token=(
            f"{GRAPH_BASE_URL}/drives/{drive_id}/root/delta?token=latest"
        ))
        del items  # token=latest returns no items by contract
        return delta_link

    def _should_do_full_sync(self, cursor: SyncCursor | None) -> Tuple[bool, str]:
        cursor_data = cursor.data if cursor else {}
        if not cursor_data:
            return True, "no cursor data (first sync)"

        schema = OneDriveCursor(**cursor_data)
        if schema.needs_full_sync():
            return True, "full_sync_required flag set or no delta tokens"

        if schema.needs_periodic_full_sync():
            return True, "periodic full sync needed (>7 days since last)"

        return False, "incremental sync (valid delta tokens)"

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all OneDrive entities using full, targeted, or incremental sync."""
        cursor_data = cursor.data if cursor else {}
        for group_ref in cursor_data.get("tracked_entra_groups") or []:
            self._tracked_entra_groups.add(str(group_ref))

        selected_folder_ids = self._selected_folder_ids(node_selections)
        is_full, reason = self._should_do_full_sync(cursor)
        scope_label = f"TARGETED ({len(selected_folder_ids)} folder roots) " if selected_folder_ids else ""
        self.logger.info(
            f"Sync strategy: {scope_label}{'FULL' if is_full else 'INCREMENTAL'} ({reason})"
        )

        if is_full:
            async for entity in self._full_sync(cursor, files, selected_folder_ids):
                yield entity
        else:
            async for entity in self._incremental_sync(cursor, files, selected_folder_ids):
                yield entity

        if cursor:
            cursor.update(tracked_entra_groups=sorted(self._tracked_entra_groups))

    async def _full_sync(
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected_folder_ids: List[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Crawl the drive (or the selected subtrees) and seed the next delta run."""
        drive_entity = None
        async for drive in self._generate_drive_entity():
            yield drive
            drive_entity = drive
            break

        if not drive_entity:
            self.logger.warning("No drive found for user")
            return

        drive_id = drive_entity.id
        drive_name = drive_entity.name or drive_entity.drive_type or "OneDrive"

        # Minted before the crawl on purpose — see _get_latest_delta_token. The app
        # folder is a virtual drive with no delta feed, so it stays full-scan only.
        pre_crawl_token = ""
        if cursor and drive_id != APP_FOLDER_DRIVE_ID:
            try:
                pre_crawl_token = await self._get_latest_delta_token(drive_id)
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Could not mint delta token for drive {drive_id}: {e}; "
                    "the next sync will crawl again"
                )

        self.logger.debug(f"Starting to process files from drive: {drive_id} ({drive_name})")

        entity_count = 0
        async for file_entity in self._generate_drive_item_entities(
            drive_id, drive_name, files=files, start_folder_ids=selected_folder_ids or None
        ):
            yield file_entity
            entity_count += 1

        if cursor and pre_crawl_token:
            schema = OneDriveCursor(**cursor.data)
            schema.update_entity_cursor(
                drive_id=drive_id,
                delta_token=pre_crawl_token,
                changes_count=entity_count,
                is_full_sync=True,
            )
            schema.synced_drive_ids[drive_id] = drive_name
            cursor.update(**schema.model_dump())

        self.logger.info(f"Full sync complete: {entity_count} entities")

    async def _incremental_sync(  # noqa: C901
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected_folder_ids: List[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Drain each drive's delta feed and yield only what changed."""
        cursor_data = cursor.data if cursor else {}
        schema = OneDriveCursor(**cursor_data)
        delta_tokens = dict(schema.drive_delta_tokens)

        if not delta_tokens:
            self.logger.warning("No delta tokens for incremental sync, falling back to full")
            async for entity in self._full_sync(cursor, files, selected_folder_ids):
                yield entity
            return

        label_filter = self._get_label_filter()
        changes_processed = 0

        for drive_id, token in delta_tokens.items():
            try:
                changed_items, new_token = await self._get_drive_delta(drive_id, token)
            except SourceAuthError:
                raise
            except Exception as e:
                # An expired or rejected token cannot be repaired here. Flag the fall
                # back durably so the next run crawls; pretending this run succeeded
                # would silently stop change tracking for good.
                self.logger.warning(f"Delta query failed for drive {drive_id}: {e}")
                if cursor:
                    cursor.update(full_sync_required=True)
                return

            drive_name = schema.synced_drive_ids.get(drive_id) or "OneDrive"
            self.logger.info(f"Drive {drive_id}: {len(changed_items)} changes")

            for item in changed_items:
                item_id = str(item.get("id") or "")
                if not item_id:
                    continue

                if item.get("deleted"):
                    yield self._deletion_entity(drive_id, item_id)
                    changes_processed += 1
                    continue

                if "folder" in item or "root" in item or not item.get("file"):
                    continue

                if selected_folder_ids and not await self._delta_item_in_scope(
                    drive_id, item, set(selected_folder_ids)
                ):
                    # A move out of a selected folder has to remove the old indexed
                    # copy. For a file that was always outside, this deletion matches
                    # no indexed object and is a harmless no-op.
                    yield self._deletion_entity(drive_id, item_id)
                    changes_processed += 1
                    continue

                if label_filter is not None and await label_filter.should_skip_item(
                    drive_id=drive_id,
                    item_id=item_id,
                    item_name=item.get("name", ""),
                ):
                    continue

                try:
                    download_url = self._get_download_url(drive_id, item_id)
                    file_entity = OneDriveDriveItemEntity.from_api(
                        item,
                        drive_name=drive_name,
                        drive_id=drive_id,
                        download_url=download_url,
                    )
                    if not file_entity:
                        continue
                    file_entity.access = await self._get_item_access(drive_id, item_id)
                    if files and not await self._download_file_entity(file_entity, files):
                        continue
                    yield file_entity
                    changes_processed += 1
                except SourceAuthError:
                    raise
                except Exception as e:
                    self.logger.warning(f"Skipping changed item {item.get('name', item_id)}: {e}")

            if cursor and new_token:
                current = OneDriveCursor(**cursor.data)
                current.update_entity_cursor(
                    drive_id=drive_id,
                    delta_token=new_token,
                    changes_count=changes_processed,
                )
                cursor.update(**current.model_dump())

        self.logger.info(f"Incremental sync complete: {changes_processed} changes processed")

    @staticmethod
    def _deletion_entity(drive_id: str, item_id: str) -> OneDriveDriveItemDeletionEntity:
        return OneDriveDriveItemDeletionEntity(
            drive_id=drive_id,
            item_id=item_id,
            deletion_status="removed",
            breadcrumbs=[],
        )

    async def _delta_item_in_scope(
        self,
        drive_id: str,
        item: Dict,
        selected_folder_ids: set,
    ) -> bool:
        """Whether a delta item remains under one of the configured subtree roots.

        The delta feed covers the whole drive, so a scoped connection has to walk the
        item's ancestry. Unknown scope fails closed: treating it as selected could
        publish a file from a folder the operator deliberately left out, and the
        periodic full crawl restores anything wrongly dropped.
        """
        parent_id = str((item.get("parentReference") or {}).get("id") or "")
        visited: set = set()
        while parent_id and parent_id not in visited:
            if parent_id in selected_folder_ids:
                return True
            visited.add(parent_id)
            try:
                parent = await self._get(
                    f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}",
                    params={"$select": "id,parentReference"},
                )
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Could not resolve delta ancestry for {item.get('id')}: {e}; "
                    "treating it as outside the selected scope"
                )
                return False
            parent_id = str((parent.get("parentReference") or {}).get("id") or "")
        return False

    # ------------------------------------------------------------------------- browse

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """List the drive's folders so an operator can pick which subtrees to sync.

        ``None`` lists the drive root; otherwise the children of that folder. Only
        folders are offered: a root means "this folder and everything below it", which
        is not something a single file can be.
        """
        drive_obj = await self._get_user_drive() or await self._create_app_folder_drive()
        drive_id = drive_obj["id"]

        nodes: List[BrowseNode] = []
        async for item in self._list_drive_items(drive_id, parent_node_id):
            folder = item.get("folder")
            if not folder:
                continue
            child_count = folder.get("childCount")
            nodes.append(
                BrowseNode(
                    source_node_id=item["id"],
                    node_type="folder",
                    title=item.get("name", item["id"]),
                    item_count=child_count,
                    # Graph omits childCount on some drives; assume expandable rather
                    # than hiding a subtree the operator may need to reach.
                    has_children=bool(child_count) if child_count is not None else True,
                    node_metadata={"drive_id": drive_id, "folder_id": item["id"]},
                )
            )
        return nodes

    def parse_browse_node_id(self, node_id: str) -> tuple:
        """OneDrive browse ids are bare drive-item ids, so there is nothing to decode."""
        return "folder", {"folder_id": node_id}

    async def validate(self) -> None:
        """Validate OneDrive credentials with drive access fallback."""
        await self._get(f"{GRAPH_BASE_URL}/me/drive")
