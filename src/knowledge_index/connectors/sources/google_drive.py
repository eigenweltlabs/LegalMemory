"""Google Drive source implementation.

Retrieves data from a user's Google Drive (read-only mode):
  - Shared drives (Drive objects)
  - Files within each shared drive
  - Files in the user's "My Drive" (non-shared, corpora=user)

Follows the same structure and pattern as other connector implementations
(e.g., Gmail, Asana, Todoist, HubSpot). The entity schemas are defined in
entities/google_drive.py.

References:
    https://developers.google.com/drive/api/v3/reference/drives (Shared drives)
    https://developers.google.com/drive/api/v3/reference/files  (Files)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import quote

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.acl import drive_permissions_to_access
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.configs import GoogleDriveConfig
from knowledge_index.connectors.cursors import GoogleDriveCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.google_drive import (
    GoogleDriveDriveEntity,
    GoogleDriveFileDeletionEntity,
    GoogleDriveFileEntity,
    _parse_drive_dt,
)
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.errors import (
    FileSkippedException,
    SourceAuthError,
    SourceEntityForbiddenError,
    SourceEntityNotFoundError,
    SourceError,
)
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.types import (
    AuthenticationMethod,
    BrowseNode,
    MembershipTuple,
    NodeSelectionData,
    OAuthType,
    RateLimitLevel,
)

# My Drive sharing permissions can be asked for inline with file metadata. Google does
# not populate ``files.permissions`` for Shared Drive items, so those are hydrated with
# ``permissions.list`` before an entity is built.
PERMISSION_FIELDS = ",permissions(id,type,role,emailAddress,domain,deleted,pendingOwner)"
PERMISSION_LIST_FIELDS = (
    "nextPageToken,permissions("
    "id,type,role,emailAddress,domain,deleted,pendingOwner,"
    "permissionDetails(inherited,inheritedFrom,permissionType,role)"
    ")"
)


# Hosts the firm's Google token may be sent to.
GOOGLE_AUTH_HOSTS = ("googleapis.com",)
GOOGLE_DIRECTORY_GROUPS_URL = "https://admin.googleapis.com/admin/directory/v1/groups"


@source(
    name="Google Drive",
    short_name="google_drive",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
        AuthenticationMethod.OAUTH_BYOC,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    requires_byoc=True,
    auth_config_class=None,
    config_class=GoogleDriveConfig,
    labels=["File Storage"],
    supports_continuous=True,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
    cursor_class=GoogleDriveCursor,
)
class GoogleDriveSource(BaseSource):
    """Google Drive source connector integrates with the Google Drive API to extract files.

    Supports both personal Google Drive (My Drive) and shared drives.

    It supports downloading and processing files
    while maintaining proper organization and access permissions.
    """

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: GoogleDriveConfig,
    ) -> GoogleDriveSource:
        """Create a new Google Drive source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance.include_patterns = config.include_patterns if config else []
        instance._mirror_permissions = bool(config.mirror_permissions) if config else True
        instance._permission_fields = PERMISSION_FIELDS if instance._mirror_permissions else ""
        # A full scan records only groups carried by files that are actually in scope.
        # After the scan the membership hook expands exactly these groups through the
        # customer's Workspace directory instead of crawling the whole tenant.
        instance._referenced_google_groups: set[str] = set()
        # Scoping state, resolved once per run — see _resolve_scope_folder_ids.
        instance._scoped = False
        instance._scope_folder_ids = set()
        instance.batch_size = 30
        instance.batch_generation = True
        instance.max_queue_size = 200
        instance.preserve_order = False
        instance.stop_on_error = False
        return instance

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        """Parse Google Drive RFC3339 timestamps into aware datetimes."""
        return _parse_drive_dt(value)

    async def validate(self) -> None:
        """Validate both the content grant and the directory grant used by ACLs."""
        await self._get(
            "https://www.googleapis.com/drive/v3/drives",
            params={"pageSize": "1"},
        )
        if self._mirror_permissions:
            # A successful empty response is enough. This proves the authorizing account
            # has the Workspace "Groups > Read API" privilege before a long content scan
            # discovers its first group-shared file.
            await self._get(
                GOOGLE_DIRECTORY_GROUPS_URL,
                params={
                    "customer": "my_customer",
                    "maxResults": "1",
                    "fields": "groups(id),nextPageToken",
                },
            )

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[Dict] = None) -> Dict:
        """Make an authenticated GET request to the Google Drive API with retry logic.

        Retries on:
        - 429 rate limits (respects Retry-After header from both real API and HttpClient)
        - Timeout errors (exponential backoff)

        Max 5 attempts with intelligent wait strategy.
        """
        token = await self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = await self.http_client.get(url, headers=headers, params=params, timeout=30.0)

        if response.status_code == 401 and self.auth.supports_refresh:
            new_token = await self.auth.force_refresh()
            headers = {"Authorization": f"Bearer {new_token}"}
            response = await self.http_client.get(url, headers=headers, params=params, timeout=30.0)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    async def _list_drives(self) -> AsyncGenerator[Dict, None]:
        """List all shared drives (Drive objects) using pagination.

        GET https://www.googleapis.com/drive/v3/drives
        """
        url = "https://www.googleapis.com/drive/v3/drives"
        params = {"pageSize": 100}
        while url:
            data = await self._get(url, params=params)
            drives = data.get("drives", [])
            self.logger.debug(f"List drives page: returned {len(drives)} drives")
            for drive_obj in drives:
                yield drive_obj

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
            params["pageToken"] = next_page_token
            url = "https://www.googleapis.com/drive/v3/drives"

    async def _hydrate_shared_drive_permissions(
        self,
        file_obj: Dict,
        *,
        drive_id: Optional[str] = None,
    ) -> None:
        """Attach the complete ACL for one Shared Drive item.

        The Files resource omits its inline ``permissions`` field for Shared Drive
        items. ``permissions.list`` is the supported source of truth and returns at
        most 100 permissions per page for those items.
        """
        if not self._mirror_permissions:
            return

        resolved_drive_id = str(file_obj.get("driveId") or drive_id or "").strip()
        file_id = str(file_obj.get("id") or "").strip()
        if not resolved_drive_id or not file_id:
            return

        file_obj["driveId"] = resolved_drive_id
        url = f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}/permissions"
        params: Dict[str, str] = {
            "pageSize": "100",
            "supportsAllDrives": "true",
            "fields": PERMISSION_LIST_FIELDS,
        }
        permissions: list[dict] = []
        try:
            while True:
                payload = await self._get(url, params=params)
                permissions.extend(payload.get("permissions") or [])
                next_page_token = str(payload.get("nextPageToken") or "").strip()
                if not next_page_token:
                    file_obj["permissions"] = permissions
                    return
                params["pageToken"] = next_page_token
        except SourceAuthError:
            raise
        except Exception as exc:
            # Never preserve a stale inline ACL after a failed refresh: that could keep
            # access alive after revocation. Unknown stays fail-closed.
            file_obj.pop("permissions", None)
            self.logger.warning(
                "Could not read Shared Drive permissions for file %s; it remains "
                "fail-closed: %s",
                file_id,
                exc,
            )

    def _build_drive_entity(self, drive_obj: Dict) -> GoogleDriveDriveEntity:
        """Build a GoogleDriveDriveEntity from API response."""
        created_time = self._parse_datetime(drive_obj.get("createdTime"))
        return GoogleDriveDriveEntity(
            breadcrumbs=[],
            drive_id=drive_obj["id"],
            title=drive_obj.get("name", "Untitled Drive"),
            created_time=created_time,
            kind=drive_obj.get("kind"),
            color_rgb=drive_obj.get("colorRgb"),
            hidden=drive_obj.get("hidden", False),
            org_unit_id=drive_obj.get("orgUnitId"),
        )

    async def _generate_drive_entities(self) -> AsyncGenerator[GoogleDriveDriveEntity, None]:
        """Generate GoogleDriveDriveEntity objects for each shared drive."""
        async for drive_obj in self._list_drives():
            yield GoogleDriveDriveEntity(
                entity_id=drive_obj["id"],
                breadcrumbs=[],
                name=drive_obj.get("name", "Untitled Drive"),
                created_at=drive_obj.get("createdTime"),
                updated_at=None,
                kind=drive_obj.get("kind"),
                color_rgb=drive_obj.get("colorRgb"),
                hidden=drive_obj.get("hidden", False),
                org_unit_id=drive_obj.get("orgUnitId"),
            )

    # --- Changes API helpers ---
    async def _get_start_page_token(self) -> str:
        url = "https://www.googleapis.com/drive/v3/changes/startPageToken"
        params = {
            "supportsAllDrives": "true",
        }
        data = await self._get(url, params=params)
        token = data.get("startPageToken")
        if not token:
            raise ValueError("Failed to retrieve startPageToken from Drive API")
        return token

    async def _iterate_changes(self, start_token: str) -> AsyncGenerator[Dict, None]:
        """Iterate over all changes since the provided page token.

        Yields individual change objects. Stores the latest newStartPageToken on the instance
        for use after the stream completes.
        """
        url = "https://www.googleapis.com/drive/v3/changes"
        params: Dict[str, Any] = {
            "pageToken": start_token,
            "includeRemoved": "true",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "pageSize": 1000,
            "fields": (
                "nextPageToken,newStartPageToken,"
                "changes(removed,fileId,changeType,file("
                "id,name,mimeType,description,trashed,explicitlyTrashed,driveId,"
                "parents,shared,webViewLink,iconLink,createdTime,modifiedTime,size,md5Checksum"
                f"{self._permission_fields})"
                ")"
            ),
        }

        latest_new_start: Optional[str] = None

        while True:
            data = await self._get(url, params=params)
            for change in data.get("changes", []) or []:
                yield change

            next_token = data.get("nextPageToken")
            latest_new_start = data.get("newStartPageToken") or latest_new_start

            if next_token:
                params["pageToken"] = next_token
            else:
                break

        self._latest_new_start_page_token = latest_new_start

    def _get_cursor_start_page_token(self) -> Optional[str]:
        """Return the stored startPageToken if available."""
        if not self._cursor:
            return None
        token = self._cursor.data.get("start_page_token")
        if not token:
            return None
        return token

    def _has_file_changed(self, file_obj: Dict) -> bool:
        """Check if file metadata indicates change without downloading.

        Compares: modifiedTime, md5Checksum, size
        Returns True if file is new or changed, False if unchanged.

        Args:
            file_obj: File metadata from Google Drive API

        Returns:
            True if file should be processed (new or changed), False if unchanged
        """
        if not self._cursor:
            return True

        file_id = file_obj.get("id")
        if not file_id:
            return True

        cursor_data = self._cursor.data
        file_metadata = cursor_data.get("file_metadata", {})
        stored_meta = file_metadata.get(file_id)

        if not stored_meta:
            return True

        current_modified = file_obj.get("modifiedTime")
        current_md5 = file_obj.get("md5Checksum")
        current_size = file_obj.get("size")
        current_permissions = self._permission_signature(file_obj)

        if (
            stored_meta.get("modified_time") != current_modified
            or stored_meta.get("md5_checksum") != current_md5
            or stored_meta.get("size") != current_size
            or stored_meta.get("permission_signature") != current_permissions
        ):
            return True

        return False

    @staticmethod
    def _permission_signature(file_obj: Dict) -> Optional[str]:
        """Return a stable ACL signature for incremental permission-only changes."""
        permissions = file_obj.get("permissions")
        if permissions is None:
            return None
        try:
            normalized = sorted(
                permissions,
                key=lambda permission: json.dumps(
                    permission,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None

    def _store_file_metadata(self, file_obj: Dict) -> None:
        """Store file metadata in cursor for future change detection.

        Args:
            file_obj: File metadata from Google Drive API
        """
        if not self._cursor:
            return

        file_id = file_obj.get("id")
        if not file_id:
            return

        cursor_data = self._cursor.data
        file_metadata = cursor_data.get("file_metadata", {})

        file_metadata[file_id] = {
            "modified_time": file_obj.get("modifiedTime"),
            "md5_checksum": file_obj.get("md5Checksum"),
            "size": file_obj.get("size"),
            "permission_signature": self._permission_signature(file_obj),
        }

        self._cursor.update(file_metadata=file_metadata)

    async def _emit_changes_since_token(
        self,
        start_token: str,
        files: FileService | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Emit change entities (modifications, additions, and deletions) since the given token."""
        self.logger.info(
            f"Processing Drive changes since token {start_token[:20]}... (incremental sync)"
        )
        self._latest_new_start_page_token = None
        try:
            async for change in self._iterate_changes(start_token):
                entity = await self._build_entity_from_change(change, files=files)
                if entity:
                    yield entity
        except SourceAuthError:
            raise
        except SourceError as exc:
            if "HTTP 410" in str(exc):
                self.logger.warning(
                    "Stored startPageToken is no longer valid (410). Fetching a fresh token."
                )
                if self._cursor:
                    try:
                        fresh_token = await self._get_start_page_token()
                        if fresh_token:
                            self._cursor.update(start_page_token=fresh_token)
                    except Exception as token_error:
                        self.logger.warning(
                            f"Failed to refresh startPageToken after 410: {token_error}"
                        )
            else:
                raise

    def _build_deletion_entity_from_change(
        self, change: Dict
    ) -> Optional[GoogleDriveFileDeletionEntity]:
        """Build a deletion entity from a Drive change object.

        Args:
            change: Change object from Google Drive Changes API

        Returns:
            GoogleDriveFileDeletionEntity if this is a valid deletion, None otherwise
        """
        file_obj = change.get("file") or {}
        file_id = change.get("fileId") or file_obj.get("id")

        if not file_id:
            self.logger.debug(
                "Drive change marked as deletion but missing fileId. Raw change: %s", change
            )
            return None

        # A removed or moved-out object must stop matching the metadata cache. If it is
        # restored or moved back into a selected folder later, identical bytes are still
        # a new source observation and its ACL/path must be rebuilt.
        if self._cursor:
            metadata = dict(self._cursor.data.get("file_metadata") or {})
            metadata.pop(str(file_id), None)
            self._cursor.update(file_metadata=metadata)
        return GoogleDriveFileDeletionEntity.from_api(change)

    async def _build_entity_from_change(
        self,
        change: Dict,
        files: FileService | None = None,
    ) -> Optional[BaseEntity]:
        """Convert a Drive change object into an entity (file or deletion).

        Handles both deletions and modifications/additions. Uses metadata comparison
        to avoid downloading unchanged files during incremental sync.

        Args:
            change: Change object from Google Drive Changes API
            files: Optional file service for downloading changed files

        Returns:
            GoogleDriveFileEntity for changed files, GoogleDriveFileDeletionEntity for deletions,
            or None if file is unchanged or should be skipped
        """
        file_obj = change.get("file") or {}
        removed = change.get("removed", False)
        trashed = bool(file_obj.get("trashed")) or bool(file_obj.get("explicitlyTrashed"))
        change_type = change.get("changeType")

        is_deletion = removed or trashed or (change_type and change_type.lower() == "removed")
        if is_deletion:
            return self._build_deletion_entity_from_change(change)

        if not file_obj.get("id"):
            return None

        if file_obj.get("mimeType") == "application/vnd.google-apps.folder":
            return None

        # The changes feed is drive-wide, so this is where scoping is enforced
        # incrementally: without it a scoped connection would pick up every change in
        # the firm's Drive on the second and every later sync.
        if not self._in_scope(file_obj):
            self.logger.debug(
                f"Change outside the selected folders, removing any previously indexed "
                f"copy: {file_obj.get('name')}"
            )
            # It may have been moved out of the selected subtree. Emitting a deletion is
            # harmless when it was never indexed and essential when it was: silently
            # skipping left the old copy searchable forever. A later move back in is
            # observed as an upsert because the metadata cache is cleared above.
            return self._build_deletion_entity_from_change(
                {**change, "fileId": file_obj["id"], "removed": True}
            )

        await self._hydrate_shared_drive_permissions(file_obj)

        if not self._has_file_changed(file_obj):
            self.logger.debug(f"File {file_obj.get('name')} unchanged (metadata match) - skipping")
            return None

        self.logger.debug(f"File {file_obj.get('name')} changed - processing")
        return await self._process_changed_file(file_obj, files=files)

    async def _store_next_start_page_token(self) -> None:
        """Persist the next startPageToken for future incremental runs."""
        if not self._cursor:
            return

        next_token = getattr(self, "_latest_new_start_page_token", None)
        if not next_token:
            try:
                next_token = await self._get_start_page_token()
            except Exception as exc:
                self.logger.warning(f"Failed to fetch startPageToken: {exc}")
                return

        if next_token:
            self._cursor.update(start_page_token=next_token)
            self.logger.debug(f"Saved startPageToken for next run: {next_token}")

    async def _list_files(
        self,
        corpora: str,
        include_all_drives: bool,
        drive_id: Optional[str] = None,
        context: str = "",
    ) -> AsyncGenerator[Dict, None]:
        """Generic method to list files with configurable parameters.

        Args:
            corpora: Google Drive API corpora parameter ("drive" or "user")
            include_all_drives: Whether to include items from all drives
            drive_id: ID of the shared drive to list files from (only for corpora="drive")
            context: Context string for logging
        """
        url = "https://www.googleapis.com/drive/v3/files"
        permission_fields = "" if drive_id else self._permission_fields
        params = {
            "pageSize": 100,
            "corpora": corpora,
            "includeItemsFromAllDrives": str(include_all_drives).lower(),
            "supportsAllDrives": "true",
            "q": "mimeType != 'application/vnd.google-apps.folder'",
            "fields": "nextPageToken, files(id, name, mimeType, description, starred, trashed, "
            "explicitlyTrashed, driveId, parents, shared, webViewLink, iconLink, createdTime, "
            f"modifiedTime, size, md5Checksum, webContentLink{permission_fields})",
        }

        if drive_id:
            params["driveId"] = drive_id

        self.logger.debug(
            f"List files start: corpora={corpora}, include_all_drives={include_all_drives}, "
            f"drive_id={drive_id}, base_q={params['q']}, context={context}"
        )

        total_files_from_api = 0
        page_count = 0

        while url:
            try:
                data = await self._get(url, params=params)
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(f"Error fetching files: {str(e)}")
                break

            files_in_page = data.get("files", [])
            page_count += 1
            files_count = len(files_in_page)
            total_files_from_api += files_count

            self.logger.debug(
                f"Google Drive API returned {files_count} files in page {page_count} ({context})"
            )

            for file_obj in files_in_page:
                await self._hydrate_shared_drive_permissions(file_obj, drive_id=drive_id)
                yield file_obj

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
            params["pageToken"] = next_page_token
            url = "https://www.googleapis.com/drive/v3/files"

        self.logger.debug(
            f"Google Drive API returned {total_files_from_api} total files across "
            f"{page_count} pages ({context})"
        )

    async def _list_folders(
        self,
        corpora: str,
        include_all_drives: bool,
        drive_id: Optional[str],
        parent_id: Optional[str],
    ) -> AsyncGenerator[Dict, None]:
        """List folders under a given parent.

        If parent_id is None, returns all folders matching name in the scope.
        """
        url = "https://www.googleapis.com/drive/v3/files"
        params = {
            "pageSize": 100,
            "corpora": corpora,
            "includeItemsFromAllDrives": str(include_all_drives).lower(),
            "supportsAllDrives": "true",
            "fields": "nextPageToken, files(id, name, driveId, parents)",
        }

        if parent_id:
            q = (
                f"'{parent_id}' in parents and "
                "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            )
        else:
            q = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        params["q"] = q

        if drive_id:
            params["driveId"] = drive_id

        self.logger.debug(
            (
                "List folders start: parent_id=%s, corpora=%s, drive_id=%s, q=%s"
                % (parent_id, corpora, drive_id, q)
            )
        )

        while url:
            data = await self._get(url, params=params)
            folders = data.get("files", [])
            self.logger.debug(
                f"List folders page: parent_id={parent_id}, returned {len(folders)} folders"
            )
            for folder in folders:
                yield folder

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
            params["pageToken"] = next_page_token
            url = "https://www.googleapis.com/drive/v3/files"

    async def _list_files_in_folder(
        self,
        corpora: str,
        include_all_drives: bool,
        drive_id: Optional[str],
        parent_id: str,
        name_token: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """List files directly under a given folder.

        Optionally coarse filtered by a "name contains" token.
        """
        url = "https://www.googleapis.com/drive/v3/files"
        permission_fields = "" if drive_id else self._permission_fields
        base_q = (
            f"'{parent_id}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        if name_token:
            safe_token = name_token.replace("'", "\\'")
            q = f"{base_q} and name contains '{safe_token}'"
        else:
            q = base_q

        params = {
            "pageSize": 100,
            "corpora": corpora,
            "includeItemsFromAllDrives": str(include_all_drives).lower(),
            "supportsAllDrives": "true",
            "q": q,
            "fields": (
                "nextPageToken, files("
                "id, name, mimeType, description, starred, trashed, driveId, "
                "explicitlyTrashed, parents, shared, webViewLink, iconLink, "
                "createdTime, modifiedTime, size, md5Checksum, webContentLink"
                f"{permission_fields})"
            ),
        }
        if drive_id:
            params["driveId"] = drive_id

        self.logger.debug(
            f"List files-in-folder start: parent_id={parent_id}, name_token={name_token}, q={q}"
        )

        while url:
            data = await self._get(url, params=params)
            files_in_page = data.get("files", [])
            self.logger.debug(
                (
                    "List files-in-folder page: parent_id=%s, returned %d files"
                    % (parent_id, len(files_in_page))
                )
            )
            for f in files_in_page:
                await self._hydrate_shared_drive_permissions(f, drive_id=drive_id)
                yield f

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
            params["pageToken"] = next_page_token
            url = "https://www.googleapis.com/drive/v3/files"

    def _extract_name_token_from_glob(self, pattern: str) -> Optional[str]:
        """Extract a coarse token for name contains from a glob (best-effort)."""
        import re

        # '*.pdf' -> '.pdf', 'report*' -> 'report'
        if pattern.startswith("*."):
            return pattern[1:]
        m = re.match(r"([^*?]+)[*?].*", pattern)
        if m:
            return m.group(1)
        if "*" not in pattern and "?" not in pattern and pattern:
            return pattern
        return None

    async def _resolve_pattern_to_roots(  # noqa: C901
        self,
        corpora: str,
        include_all_drives: bool,
        drive_id: Optional[str],
        pattern: str,
    ) -> tuple[List[str], Optional[str]]:
        """Resolve a pattern like 'FOLDER/SUBFOLDER/*.pdf' to root folder IDs and filename glob.

        Supports patterns like: 'Folder/*', 'Folder/Sub/file.pdf'.
        Folder segments are treated as exact names.
        The last segment may be a filename glob; if omitted, includes all files recursively.
        """
        self.logger.debug(f"Resolve pattern: '{pattern}'")
        norm = pattern.strip().strip("/")
        segments = norm.split("/") if norm else []

        if not segments:
            return [], None

        last = segments[-1]
        filename_glob: Optional[str] = None
        folder_segments = segments
        if "." in last or "*" in last or "?" in last:
            filename_glob = last
            folder_segments = segments[:-1]
        self.logger.debug(
            f"Pattern segments: folders={folder_segments}, filename_glob={filename_glob}"
        )

        async def find_folders_by_name(parent_ids: Optional[List[str]], name: str) -> List[str]:  # noqa: C901
            """Find folders by exact name, either under specific parents or globally."""
            found: List[str] = []
            safe_name = name.replace("'", "\\'")

            if parent_ids:
                for pid in parent_ids:
                    url = "https://www.googleapis.com/drive/v3/files"
                    q = (
                        f"'{pid}' in parents and mimeType = 'application/vnd.google-apps.folder' "
                        f"and name = '{safe_name}' and trashed = false"
                    )
                    params = {
                        "pageSize": 100,
                        "corpora": corpora,
                        "includeItemsFromAllDrives": str(include_all_drives).lower(),
                        "supportsAllDrives": "true",
                        "q": q,
                        "fields": "nextPageToken, files(id, name)",
                    }
                    if drive_id:
                        params["driveId"] = drive_id

                    while url:
                        data = await self._get(url, params=params)
                        for f in data.get("files", []):
                            found.append(f["id"])
                        npt = data.get("nextPageToken")
                        if not npt:
                            break
                        params["pageToken"] = npt

                self.logger.debug(
                    f"find_folders_by_name: name='{name}' under {len(parent_ids)} "
                    f"parents -> {len(found)} matches"
                )
            else:
                url = "https://www.googleapis.com/drive/v3/files"
                q = (
                    "mimeType = 'application/vnd.google-apps.folder' and "
                    f"name = '{safe_name}' and trashed = false"
                )
                params = {
                    "pageSize": 100,
                    "corpora": corpora,
                    "includeItemsFromAllDrives": str(include_all_drives).lower(),
                    "supportsAllDrives": "true",
                    "q": q,
                    "fields": "nextPageToken, files(id, name)",
                }
                if drive_id:
                    params["driveId"] = drive_id

                while url:
                    data = await self._get(url, params=params)
                    for f in data.get("files", []):
                        found.append(f["id"])
                    npt = data.get("nextPageToken")
                    if not npt:
                        break
                    params["pageToken"] = npt

                self.logger.debug(
                    f"find_folders_by_name: global name='{name}' -> {len(found)} matches"
                )
            return found

        parent_ids: Optional[List[str]] = None
        for seg in folder_segments:
            ids = await find_folders_by_name(parent_ids, seg)
            parent_ids = ids
            if not parent_ids:
                break

        if not folder_segments:
            return [], filename_glob or "*"

        self.logger.debug(
            f"Resolved pattern '{pattern}' to {len(parent_ids or [])} folder(s), "
            f"filename_glob={filename_glob}"
        )
        return parent_ids or [], filename_glob

    async def _traverse_and_yield_files(
        self,
        corpora: str,
        include_all_drives: bool,
        drive_id: Optional[str],
        start_folder_ids: List[str],
        filename_glob: Optional[str],
        context: str,
    ) -> AsyncGenerator[Dict, None]:
        """BFS traversal from start folders yielding file objects.

        Final match is performed by filename glob.
        """
        import fnmatch
        from collections import deque

        name_token = self._extract_name_token_from_glob(filename_glob) if filename_glob else None

        self.logger.debug(
            f"Traverse start: roots={len(start_folder_ids)}, filename_glob={filename_glob}, "
            f"name_token={name_token}"
        )

        queue = deque(start_folder_ids)
        while queue:
            folder_id = queue.popleft()

            self.logger.debug(f"Scanning folder: {folder_id}")
            async for file_obj in self._list_files_in_folder(
                corpora, include_all_drives, drive_id, folder_id, name_token
            ):
                file_name = file_obj.get("name", "")
                if filename_glob:
                    matched = fnmatch.fnmatch(file_name, filename_glob)
                    self.logger.debug(
                        f"Encountered file: {file_name} ({file_obj.get('id')}) "
                        f"matched={matched} pattern={filename_glob}"
                    )
                    if matched:
                        yield file_obj
                else:
                    self.logger.debug(
                        f"Encountered file: {file_name} ({file_obj.get('id')}) matched=True"
                    )
                    yield file_obj

            async for subfolder in self._list_folders(
                corpora, include_all_drives, drive_id, folder_id
            ):
                self.logger.debug(
                    f"Enqueue subfolder: {subfolder.get('name')} ({subfolder.get('id')})"
                )
                queue.append(subfolder["id"])

    # ------------------------------
    # Subtree scoping
    # ------------------------------
    async def _resolve_scope_folder_ids(
        self, node_selections: Optional[List[NodeSelectionData]]
    ) -> set[str]:
        """Expand the selected roots into every folder id they cover.

        Drive has no folder-scoped listing — ``files.list`` is drive-wide and the changes
        feed more so — so scoping has to be done by ancestry. The descendant set is walked
        once per run and cached, and because it is walked fresh each run a matter folder
        created since the last sync is picked up without anyone re-choosing the scope.
        """
        from collections import deque

        roots: List[str] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            folder_id = str(metadata.get("folder_id") or selection.source_node_id or "").strip()
            if folder_id and folder_id not in roots:
                roots.append(folder_id)

        resolved: set[str] = set()
        queue = deque()
        for folder_id in roots:
            try:
                await self._get(
                    f"https://www.googleapis.com/drive/v3/files/{folder_id}",
                    params={"fields": "id,name,mimeType", "supportsAllDrives": "true"},
                )
            except SourceAuthError:
                raise
            except Exception as exc:
                # Never fall back to the whole drive: an operator who scoped this
                # connection to one matter folder must not silently get a partner's
                # entire Drive because that folder was deleted or unshared.
                self.logger.warning(
                    f"Selected folder {folder_id} could not be read ({exc}); skipping it — "
                    "the remaining selected folders still sync"
                )
                continue
            resolved.add(folder_id)
            queue.append(folder_id)

        while queue:
            parent_id = queue.popleft()
            try:
                async for folder in self._list_folders("allDrives", True, None, parent_id):
                    child_id = folder.get("id")
                    if child_id and child_id not in resolved:
                        resolved.add(child_id)
                        queue.append(child_id)
            except SourceAuthError:
                raise
            except Exception as exc:
                self.logger.warning(f"Could not expand selected folder {parent_id}: {exc}")

        return resolved

    def _in_scope(self, file_obj: Dict) -> bool:
        """Whether a file sits under one of the selected roots.

        Unscoped connections answer True for everything, which is the unchanged
        behaviour. A scoped connection whose roots all failed to resolve answers False
        for everything — nothing is indexed, rather than everything.
        """
        if not self._scoped:
            return True
        return any(parent in self._scope_folder_ids for parent in file_obj.get("parents") or [])

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """List the folders under a parent so an operator can pick sync roots.

        ``None`` lists My Drive's top-level folders plus each Shared Drive. A Shared
        Drive is also a folder root in the Files API, so selecting or opening it uses
        the same subtree-scoping path as any other folder.
        """
        parent_id = parent_node_id or "root"
        nodes: List[BrowseNode] = []
        async for folder in self._list_folders("allDrives", True, None, parent_id):
            folder_id = folder.get("id")
            if not folder_id:
                continue
            nodes.append(
                BrowseNode(
                    source_node_id=folder_id,
                    node_type="folder",
                    title=folder.get("name", folder_id),
                    # Drive reports no child count on a folder listing, so every folder
                    # stays expandable rather than looking empty in the picker.
                    has_children=True,
                    node_metadata={
                        "folder_id": folder_id,
                        **(
                            {"drive_id": folder["driveId"]}
                            if folder.get("driveId")
                            else {}
                        ),
                    },
                )
            )
        if parent_node_id is None:
            async for drive_obj in self._list_drives():
                drive_id = str(drive_obj.get("id") or "").strip()
                if not drive_id:
                    continue
                nodes.append(
                    BrowseNode(
                        source_node_id=drive_id,
                        node_type="drive",
                        title=drive_obj.get("name", drive_id),
                        has_children=True,
                        node_metadata={"folder_id": drive_id, "drive_id": drive_id},
                    )
                )
        return nodes

    def _build_file_entity(
        self, file_obj: Dict, parent_breadcrumb: Optional[Breadcrumb]
    ) -> Optional[GoogleDriveFileEntity]:
        """Helper to build a GoogleDriveFileEntity from a file API response object.

        Returns None for files that should be skipped (e.g., trashed files, videos, and
        anything outside the selected subtrees).
        """
        if not self._in_scope(file_obj):
            self.logger.debug(
                f"Outside the selected folders, skipping: {file_obj.get('name', 'unknown')}"
            )
            return None

        mime_type = file_obj.get("mimeType", "")
        if mime_type.startswith("video/"):
            file_name = file_obj.get("name", "unknown")
            self.logger.debug(f"Skipping video file ({mime_type}): {file_name}")
            return None

        MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024
        file_size = int(file_obj["size"]) if file_obj.get("size") else 0
        if file_size > MAX_FILE_SIZE_BYTES:
            file_name = file_obj.get("name", "unknown")
            size_mb = file_size / (1024 * 1024)
            self.logger.info(f"Skipping oversized file ({size_mb:.1f}MB, max 200MB): {file_name}")
            return None

        if not mime_type.startswith("application/vnd.google-apps.") and file_obj.get(
            "trashed", False
        ):
            return None

        breadcrumbs = [parent_breadcrumb] if parent_breadcrumb else []
        if not breadcrumbs and getattr(self, "_my_drive_breadcrumb", None):
            breadcrumbs = [self._my_drive_breadcrumb]

        file_entity = GoogleDriveFileEntity.from_api(file_obj, breadcrumbs=breadcrumbs)
        if file_entity is not None:
            file_entity.access = self._access_for_file(file_obj)
        return file_entity

    def _access_for_file(self, file_obj: Dict) -> Optional[AccessControl]:
        """Translate the sharing permissions that came back with the file metadata.

        Drive omits the permissions sub-resource when the signed-in account may not see
        it. That leaves access as None, which means "unknown" and keeps the file
        fail-closed; an empty AccessControl would instead assert that nobody may read it.
        """
        if not self._mirror_permissions:
            return None
        try:
            access = drive_permissions_to_access(file_obj.get("permissions"))
            if access is not None:
                prefix = "group:google:"
                self._referenced_google_groups.update(
                    principal[len(prefix) :]
                    for principal in access.viewers
                    if principal.startswith(prefix) and principal[len(prefix) :]
                )
            return access
        except Exception as e:
            self.logger.warning(
                f"Could not read permissions for file {file_obj.get('id', 'unknown')}: {e}"
            )
            return None

    async def _list_google_group_members(self, group_email: str) -> Optional[list[dict]]:
        """Return direct and derived members for one referenced Workspace group.

        A group outside the customer's tenant may legitimately be present on a Drive ACL
        but unreadable through that tenant's Admin SDK. Only that group stays fail-closed.
        The connector-level validation has already proven that the Directory API itself
        is enabled and that the authorizing account holds the read-only admin privilege.
        """
        url = f"{GOOGLE_DIRECTORY_GROUPS_URL}/{quote(group_email, safe='')}/members"
        params: Dict[str, str] = {
            "includeDerivedMembership": "true",
            "maxResults": "200",
        }
        members: list[dict] = []
        try:
            while True:
                payload = await self._get(url, params=params)
                members.extend(payload.get("members") or [])
                next_page_token = str(payload.get("nextPageToken") or "").strip()
                if not next_page_token:
                    return members
                params["pageToken"] = next_page_token
        except SourceAuthError:
            raise
        except (SourceEntityForbiddenError, SourceEntityNotFoundError) as exc:
            self.logger.warning(
                "Could not enumerate Google Group %s; grants to this group remain "
                "fail-closed: %s",
                group_email,
                exc,
            )
            return None

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand Google Group grants into the people who may open the files.

        ``includeDerivedMembership`` makes nested groups arrive already flattened. Group
        objects in the response are ignored; user and external-person entries are written
        directly against the root group carried by the Drive ACL.
        """
        if not self._mirror_permissions:
            return

        for group_email in sorted(self._referenced_google_groups):
            members = await self._list_google_group_members(group_email)
            if members is None:
                continue
            seen: set[str] = set()
            for member in members:
                member_type = str(member.get("type") or "").strip().upper()
                email = str(member.get("email") or "").strip().casefold()
                status = str(member.get("status") or "").strip().upper()
                if (
                    member_type not in {"USER", "EXTERNAL"}
                    or not email
                    or status == "SUSPENDED"
                    or email in seen
                ):
                    continue
                seen.add(email)
                yield MembershipTuple(
                    member_id=email,
                    member_type="user",
                    group_id=f"google:{group_email}",
                    group_name=group_email,
                )

    # ------------------------------
    # File download helper
    # ------------------------------
    async def _download_file(
        self,
        file_entity: GoogleDriveFileEntity,
        files: FileService | None,
    ) -> bool:
        """Download a file via FileService. Returns True if download succeeded.

        401 propagates (dead token). Other HTTP errors log a warning.
        """
        if not files:
            return False
        try:
            await files.download_from_url(
                entity=file_entity,
                client=self.http_client,
                auth=self.auth,
                logger=self.logger,
                auth_hosts=GOOGLE_AUTH_HOSTS,
            )
            return bool(file_entity.local_path)
        except FileSkippedException as e:
            self.logger.debug(f"Skipping file {file_entity.name}: {e.reason}")
            return False
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            self.logger.warning(f"Failed to download {file_entity.name}: {e}")
            return False

    # ------------------------------
    # Concurrency-aware processing
    # ------------------------------
    async def _process_file_batch(
        self,
        file_obj: Dict,
        parent_breadcrumb: Optional[Breadcrumb],
        files: FileService | None = None,
    ) -> Optional[GoogleDriveFileEntity]:
        """Build & process a single file (used by concurrent driver)."""
        try:
            file_entity = self._build_file_entity(file_obj, parent_breadcrumb)
            if not file_entity:
                return None
            self.logger.debug(f"Processing file entity: {file_entity.file_id} '{file_entity.name}'")

            if await self._download_file(file_entity, files):
                self._store_file_metadata(file_obj)
                self.logger.debug(f"Successfully downloaded file: {file_entity.name}")
                return file_entity

            return None

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Failed to process file {file_obj.get('name', 'unknown')}: {str(e)}"
            )
            return None

    async def _process_changed_file(
        self,
        file_obj: Dict,
        parent_breadcrumb: Optional[Breadcrumb] = None,
        files: FileService | None = None,
    ) -> Optional[GoogleDriveFileEntity]:
        """Process a file that has changed based on metadata.

        This method is used during incremental sync to process files that have been
        identified as changed through metadata comparison.

        Args:
            file_obj: File metadata from Google Drive API
            parent_breadcrumb: Optional breadcrumb for parent folder
            files: Optional file service for downloading

        Returns:
            GoogleDriveFileEntity if processing succeeded, None if skipped or failed
        """
        if parent_breadcrumb is None:
            drive_id = str(file_obj.get("driveId") or "").strip()
            parent_breadcrumb = (
                getattr(self, "_drive_breadcrumbs", {}).get(drive_id)
                if drive_id
                else getattr(self, "_my_drive_breadcrumb", None)
            )
        file_entity = self._build_file_entity(file_obj, parent_breadcrumb)
        if not file_entity:
            return None

        self.logger.debug(f"Processing changed file: {file_entity.file_id} '{file_entity.name}'")

        if await self._download_file(file_entity, files):
            self._store_file_metadata(file_obj)
            self.logger.debug(f"Successfully processed changed file: {file_entity.name}")
            return file_entity

        return None

    def _setup_breadcrumbs(self, drive_objs: List[Dict[str, Any]]) -> None:
        """Setup breadcrumbs for drives and My Drive.

        Args:
            drive_objs: List of drive objects from Google Drive API
        """
        drive_breadcrumbs: Dict[str, Breadcrumb] = {}
        for drive_obj in drive_objs:
            drive_breadcrumbs[drive_obj["id"]] = Breadcrumb(
                entity_id=drive_obj["id"],
                name=drive_obj.get("name", "Untitled Drive"),
                entity_type=GoogleDriveDriveEntity.__name__,
            )

        self._drive_breadcrumbs = drive_breadcrumbs
        self._my_drive_breadcrumb = Breadcrumb(
            entity_id="my_drive",
            name="My Drive",
            entity_type=GoogleDriveDriveEntity.__name__,
        )

    async def _generate_file_entities(  # noqa: C901
        self,
        corpora: str,
        include_all_drives: bool,
        drive_id: Optional[str] = None,
        context: str = "",
        parent_breadcrumb: Optional[Breadcrumb] = None,
        files: FileService | None = None,
    ) -> AsyncGenerator[GoogleDriveFileEntity, None]:
        """Generate file entities from a file listing."""
        try:
            if getattr(self, "batch_generation", False):

                async def _worker(file_obj: Dict):
                    ent = await self._process_file_batch(file_obj, parent_breadcrumb, files)
                    if ent is not None:
                        yield ent

                async for processed in self.process_entities_concurrent(
                    items=self._list_files(corpora, include_all_drives, drive_id, context),
                    worker=_worker,
                    batch_size=getattr(self, "batch_size", 30),
                    preserve_order=getattr(self, "preserve_order", False),
                    stop_on_error=getattr(self, "stop_on_error", False),
                    max_queue_size=getattr(self, "max_queue_size", 200),
                ):
                    yield processed
            else:
                async for file_obj in self._list_files(
                    corpora, include_all_drives, drive_id, context
                ):
                    try:
                        file_entity = self._build_file_entity(file_obj, parent_breadcrumb)
                        if not file_entity:
                            continue

                        if await self._download_file(file_entity, files):
                            self._store_file_metadata(file_obj)
                            yield file_entity

                    except SourceAuthError:
                        raise
                    except Exception as e:
                        error_context = f"in drive {drive_id}" if drive_id else "in MY DRIVE"
                        self.logger.warning(
                            f"Failed to process file {file_obj.get('name', 'unknown')} "
                            f"{error_context}: {str(e)}"
                        )
                        continue

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Critical exception in _generate_file_entities: {str(e)}")

    async def generate_entities(  # noqa: C901
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all Google Drive entities.

        Behavior:
        - If no cursor token exists: perform a FULL sync (shared drives + files), then store
          the current startPageToken for the next incremental run.
        - If a cursor token exists: perform INCREMENTAL sync using the Changes API. Emit
          deletion entities for removed files and upsert entities for changed files.
        """
        self._cursor = cursor
        self._scoped = bool(node_selections)
        self._scope_folder_ids = await self._resolve_scope_folder_ids(node_selections)
        if self._scoped:
            self.logger.info(
                f"Sync strategy: TARGETED ({len(self._scope_folder_ids)} folders in scope)"
            )

        try:
            start_page_token = self._get_cursor_start_page_token()
            if start_page_token:
                self.logger.debug(f"Incremental sync using startPageToken={start_page_token}")
            else:
                self.logger.debug("Full sync (no stored startPageToken)")
                self._referenced_google_groups.clear()

            patterns: List[str] = getattr(self, "include_patterns", []) or []
            self.logger.debug(f"Include patterns: {patterns}")

            drive_objs: List[Dict[str, Any]] = []
            try:
                async for drive_obj in self._list_drives():
                    drive_objs.append(drive_obj)
                    yield self._build_drive_entity(drive_obj)
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(f"Error generating drive entities: {str(e)}")

            self._setup_breadcrumbs(drive_objs)
            drive_breadcrumbs = self._drive_breadcrumbs
            drive_ids = [drive["id"] for drive in drive_objs]

            # INCREMENTAL MODE: Use Changes API exclusively
            if start_page_token:
                self.logger.info(
                    "Incremental sync mode - processing changes only"
                    f" (token={start_page_token[:20]}...)"
                )
                async for change_entity in self._emit_changes_since_token(
                    start_page_token, files=files
                ):
                    yield change_entity

            else:
                # FULL SYNC MODE: List all files (first run or forced full sync)
                self.logger.info("Full sync mode - listing all files")

                # If no include patterns: default behavior (all files in drives + My Drive)
                if not patterns:
                    for drive_id in drive_ids:
                        try:
                            drive_breadcrumb = drive_breadcrumbs.get(drive_id)
                            async for file_entity in self._generate_file_entities(
                                corpora="drive",
                                include_all_drives=True,
                                drive_id=drive_id,
                                context=f"drive {drive_id}",
                                parent_breadcrumb=drive_breadcrumb,
                                files=files,
                            ):
                                yield file_entity
                        except SourceAuthError:
                            raise
                        except Exception as e:
                            self.logger.warning(
                                f"Error processing shared drive {drive_id}: {str(e)}"
                            )
                            continue

                    try:
                        async for mydrive_file_entity in self._generate_file_entities(
                            corpora="user",
                            include_all_drives=False,
                            context="MY DRIVE",
                            parent_breadcrumb=self._my_drive_breadcrumb,
                            files=files,
                        ):
                            yield mydrive_file_entity
                    except SourceAuthError:
                        raise
                    except Exception as e:
                        self.logger.warning(f"Error processing My Drive files: {str(e)}")

                # INCLUDE MODE: Resolve patterns and traverse only matched subtrees
                # Shared drives first
                for drive_id in drive_ids:
                    try:
                        drive_breadcrumb = drive_breadcrumbs.get(drive_id)
                        for p in patterns:
                            roots, fname_glob = await self._resolve_pattern_to_roots(
                                corpora="drive",
                                include_all_drives=True,
                                drive_id=drive_id,
                                pattern=p,
                            )
                            if roots:
                                if getattr(self, "batch_generation", False):

                                    async def _worker_traverse(
                                        file_obj: Dict, breadcrumb=drive_breadcrumb
                                    ):
                                        ent = await self._process_file_batch(
                                            file_obj, breadcrumb, files
                                        )
                                        if ent is not None:
                                            yield ent

                                    items_gen = self._traverse_and_yield_files(
                                        corpora="drive",
                                        include_all_drives=True,
                                        drive_id=drive_id,
                                        start_folder_ids=list(set(roots)),
                                        filename_glob=fname_glob,
                                        context=f"drive {drive_id}",
                                    )

                                    async for processed in self.process_entities_concurrent(
                                        items=items_gen,
                                        worker=_worker_traverse,
                                        batch_size=getattr(self, "batch_size", 30),
                                        preserve_order=getattr(self, "preserve_order", False),
                                        stop_on_error=getattr(self, "stop_on_error", False),
                                        max_queue_size=getattr(self, "max_queue_size", 200),
                                    ):
                                        yield processed
                                else:
                                    async for file_obj in self._traverse_and_yield_files(
                                        corpora="drive",
                                        include_all_drives=True,
                                        drive_id=drive_id,
                                        start_folder_ids=list(set(roots)),
                                        filename_glob=fname_glob,
                                        context=f"drive {drive_id}",
                                    ):
                                        file_entity = self._build_file_entity(
                                            file_obj, drive_breadcrumb
                                        )
                                        if not file_entity:
                                            continue

                                        try:
                                            if await self._download_file(file_entity, files):
                                                yield file_entity
                                        except SourceAuthError:
                                            raise
                                        except Exception as e:
                                            self.logger.warning(
                                                f"Download failed {file_entity.name}: {e}"
                                            )
                                            continue

                        filename_only_patterns = [p for p in patterns if "/" not in p]
                        import fnmatch as _fn

                        for pat in filename_only_patterns:
                            if getattr(self, "batch_generation", False):

                                async def _worker_match(
                                    file_obj: Dict,
                                    pattern=pat,
                                    breadcrumb=drive_breadcrumb,
                                ):
                                    name = file_obj.get("name", "")
                                    if _fn.fnmatch(name, pattern):
                                        ent = await self._process_file_batch(
                                            file_obj, breadcrumb, files
                                        )
                                        if ent is not None:
                                            yield ent

                                async for processed in self.process_entities_concurrent(
                                    items=self._list_files(
                                        corpora="drive",
                                        include_all_drives=True,
                                        drive_id=drive_id,
                                        context=f"drive {drive_id}",
                                    ),
                                    worker=_worker_match,
                                    batch_size=getattr(self, "batch_size", 30),
                                    preserve_order=getattr(self, "preserve_order", False),
                                    stop_on_error=getattr(self, "stop_on_error", False),
                                    max_queue_size=getattr(self, "max_queue_size", 200),
                                ):
                                    yield processed
                            else:
                                async for file_obj in self._list_files(
                                    corpora="drive",
                                    include_all_drives=True,
                                    drive_id=drive_id,
                                    context=f"drive {drive_id}",
                                ):
                                    name = file_obj.get("name", "")
                                    if _fn.fnmatch(name, pat):
                                        file_entity = self._build_file_entity(
                                            file_obj, drive_breadcrumb
                                        )
                                        if not file_entity:
                                            continue

                                        try:
                                            if await self._download_file(file_entity, files):
                                                yield file_entity
                                        except SourceAuthError:
                                            raise
                                        except Exception as e:
                                            self.logger.warning(
                                                f"Download failed {file_entity.name}: {e}"
                                            )
                                            continue

                    except SourceAuthError:
                        raise
                    except Exception as e:
                        self.logger.warning(f"Include mode error for drive {drive_id}: {str(e)}")

                # My Drive include patterns
                try:
                    for p in patterns:
                        roots, fname_glob = await self._resolve_pattern_to_roots(
                            corpora="user",
                            include_all_drives=False,
                            drive_id=None,
                            pattern=p,
                        )
                        if roots:
                            if getattr(self, "batch_generation", False):

                                async def _worker_traverse_user(
                                    file_obj: Dict, breadcrumb=self._my_drive_breadcrumb
                                ):
                                    ent = await self._process_file_batch(
                                        file_obj, breadcrumb, files
                                    )
                                    if ent is not None:
                                        yield ent

                                items_gen_user = self._traverse_and_yield_files(
                                    corpora="user",
                                    include_all_drives=False,
                                    drive_id=None,
                                    start_folder_ids=list(set(roots)),
                                    filename_glob=fname_glob,
                                    context="MY DRIVE",
                                )

                                async for processed in self.process_entities_concurrent(
                                    items=items_gen_user,
                                    worker=_worker_traverse_user,
                                    batch_size=getattr(self, "batch_size", 30),
                                    preserve_order=getattr(self, "preserve_order", False),
                                    stop_on_error=getattr(self, "stop_on_error", False),
                                    max_queue_size=getattr(self, "max_queue_size", 200),
                                ):
                                    yield processed
                            else:
                                async for file_obj in self._traverse_and_yield_files(
                                    corpora="user",
                                    include_all_drives=False,
                                    drive_id=None,
                                    start_folder_ids=list(set(roots)),
                                    filename_glob=fname_glob,
                                    context="MY DRIVE",
                                ):
                                    file_entity = self._build_file_entity(
                                        file_obj, self._my_drive_breadcrumb
                                    )
                                    if not file_entity:
                                        continue

                                    try:
                                        if await self._download_file(file_entity, files):
                                            yield file_entity
                                    except SourceAuthError:
                                        raise
                                    except Exception as e:
                                        self.logger.warning(
                                            f"Failed to download file {file_entity.name}: {e}"
                                        )
                                        continue

                    filename_only_patterns = [p for p in patterns if "/" not in p]
                    import fnmatch as _fn

                    for pat in filename_only_patterns:
                        if getattr(self, "batch_generation", False):

                            async def _worker_match_user(
                                file_obj: Dict,
                                pattern=pat,
                                breadcrumb=self._my_drive_breadcrumb,
                            ):
                                name = file_obj.get("name", "")
                                if _fn.fnmatch(name, pattern):
                                    ent = await self._process_file_batch(
                                        file_obj, breadcrumb, files
                                    )
                                    if ent is not None:
                                        yield ent

                            async for processed in self.process_entities_concurrent(
                                items=self._list_files(
                                    corpora="user",
                                    include_all_drives=False,
                                    drive_id=None,
                                    context="MY DRIVE",
                                ),
                                worker=_worker_match_user,
                                batch_size=getattr(self, "batch_size", 30),
                                preserve_order=getattr(self, "preserve_order", False),
                                stop_on_error=getattr(self, "stop_on_error", False),
                                max_queue_size=getattr(self, "max_queue_size", 200),
                            ):
                                yield processed
                        else:
                            async for file_obj in self._list_files(
                                corpora="user",
                                include_all_drives=False,
                                drive_id=None,
                                context="MY DRIVE",
                            ):
                                name = file_obj.get("name", "")
                                if _fn.fnmatch(name, pat):
                                    file_entity = self._build_file_entity(
                                        file_obj, self._my_drive_breadcrumb
                                    )
                                    if not file_entity:
                                        continue

                                    try:
                                        if await self._download_file(file_entity, files):
                                            yield file_entity
                                    except SourceAuthError:
                                        raise
                                    except Exception as e:
                                        self.logger.warning(
                                            f"Failed to download file {file_entity.name}: {e}"
                                        )
                                        continue

                except SourceAuthError:
                    raise
                except Exception as e:
                    self.logger.warning(f"Include mode error for My Drive: {str(e)}")

            # Store the next start page token for future incremental syncs
            await self._store_next_start_page_token()

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Critical error in generate_entities: {str(e)}")
            from knowledge_index.connectors.runtime.errors import SyncFailureError

            raise SyncFailureError(f"Google Drive sync failed: {str(e)}") from e
