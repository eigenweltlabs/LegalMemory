"""Dropbox source implementation."""

from __future__ import annotations

import json
import mimetypes
import os
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.types import RateLimitLevel
from knowledge_index.connectors.runtime.types import BrowseNode, NodeSelectionData
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.acl import dropbox_members_to_access
from knowledge_index.connectors.configs import DropboxConfig
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.dropbox import (
    DropboxAccountEntity,
    DropboxFileEntity,
    DropboxFolderEntity,
)
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.types import AuthenticationMethod, OAuthType


@source(
    name="Dropbox",
    short_name="dropbox",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    requires_byoc=True,
    auth_config_class=None,
    config_class=DropboxConfig,
    labels=["File Storage"],
    supports_continuous=False,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class DropboxSource(BaseSource):
    """Dropbox source connector integrates with the Dropbox API to extract and synchronize files.

    Connects to folder structures from your Dropbox account.

    It supports downloading and processing files.
    """

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: DropboxConfig,
    ) -> DropboxSource:
        """Create a new Dropbox source with credentials and config."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._exclude_path = config.exclude_path or ""
        instance._mirror_permissions = config.mirror_permissions
        return instance

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _post(self, url: str, json_data: Dict | None = None) -> Dict:
        """Make an authenticated POST request to the Dropbox API."""
        token = await self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}

        if json_data is not None:
            response = await self.http_client.post(url, headers=headers, json=json_data)
        else:
            response = await self.http_client.post(url, headers=headers)

        if response.status_code == 401 and self.auth.supports_refresh:
            new_token = await self.auth.force_refresh()
            headers = {"Authorization": f"Bearer {new_token}"}
            if json_data is not None:
                response = await self.http_client.post(url, headers=headers, json=json_data)
            else:
                response = await self.http_client.post(url, headers=headers)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    async def _generate_account_entities(self) -> AsyncGenerator[BaseEntity, None]:
        """Generate Dropbox account-level entities using the Dropbox API."""
        url = "https://api.dropboxapi.com/2/users/get_current_account"
        account_data = await self._post(url, None)
        yield DropboxAccountEntity.from_api(account_data)

    def _create_folder_entity(
        self, entry: Dict, account_breadcrumb: Breadcrumb
    ) -> Tuple[DropboxFolderEntity, str]:
        """Create a DropboxFolderEntity from an API response entry."""
        folder_entity = DropboxFolderEntity.from_api(entry, breadcrumbs=[account_breadcrumb])
        return folder_entity, entry.get("path_lower", "")

    async def _get_paginated_entries(
        self, url: str, initial_data: Dict, continuation_url: str | None = None
    ) -> AsyncGenerator[Dict, None]:
        """Fetch all entries from a paginated Dropbox API endpoint."""
        if continuation_url is None:
            continuation_url = url

        response_data = await self._post(url, initial_data)

        for entry in response_data.get("entries", []):
            yield entry

        while response_data.get("has_more", False):
            continue_data = {"cursor": response_data.get("cursor")}
            response_data = await self._post(continuation_url, continue_data)
            for entry in response_data.get("entries", []):
                yield entry

    async def _generate_folder_entities(
        self,
        account_breadcrumb: Breadcrumb,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate folder entities for a given Dropbox account."""
        url = "https://api.dropboxapi.com/2/files/list_folder"
        continue_url = "https://api.dropboxapi.com/2/files/list_folder/continue"

        folders_to_process: list[tuple[str, str]] = [("", "Root")]

        while folders_to_process:
            current_path, current_name = folders_to_process.pop(0)
            data = {
                "path": current_path,
                "recursive": False,
                "include_deleted": False,
                "include_has_explicit_shared_members": True,
                "include_mounted_folders": True,
                "include_non_downloadable_files": True,
            }

            async for entry in self._get_paginated_entries(url, data, continue_url):
                if entry.get(".tag") == "folder":
                    folder_entity, folder_path = self._create_folder_entity(
                        entry, account_breadcrumb
                    )
                    yield folder_entity
                    folders_to_process.append((folder_path, folder_entity.name))

    def _create_file_entity(
        self, entry: Dict, folder_breadcrumbs: List[Breadcrumb]
    ) -> DropboxFileEntity:
        """Create a DropboxFileEntity from an API response entry."""
        file_id = entry.get("id", "")
        file_path = entry.get("path_lower", "")

        client_modified = None
        server_modified = None

        if entry.get("client_modified"):
            try:
                from datetime import datetime

                client_modified = datetime.strptime(
                    entry.get("client_modified"), "%Y-%m-%dT%H:%M:%SZ"
                )
            except (ValueError, TypeError):
                pass

        if entry.get("server_modified"):
            try:
                from datetime import datetime

                server_modified = datetime.strptime(
                    entry.get("server_modified"), "%Y-%m-%dT%H:%M:%SZ"
                )
            except (ValueError, TypeError):
                pass

        sharing_info = entry.get("sharing_info", {})

        file_name = entry.get("name", "Unknown File")
        mime_type = mimetypes.guess_type(file_name)[0]
        if mime_type and "/" in mime_type:
            file_type = mime_type.split("/")[0]
        else:
            ext = os.path.splitext(file_name)[1].lower().lstrip(".")
            file_type = ext if ext else "file"

        return DropboxFileEntity(
            id=file_id if file_id else f"file-{file_path}",
            breadcrumbs=folder_breadcrumbs,
            name=file_name,
            url="https://content.dropboxapi.com/2/files/download",
            size=entry.get("size", 0),
            file_type=file_type,
            mime_type=mime_type or "application/octet-stream",
            local_path=None,
            path_lower=entry.get("path_lower"),
            path_display=entry.get("path_display"),
            rev=entry.get("rev"),
            client_modified=client_modified,
            server_modified=server_modified,
            is_downloadable=entry.get("is_downloadable", True),
            content_hash=entry.get("content_hash"),
            sharing_info=sharing_info,
            has_explicit_shared_members=entry.get("has_explicit_shared_members"),
        )

    async def _get_file_access(self, file_id: str) -> Optional[AccessControl]:
        """Read a file's sharing members so it is retrievable by whoever can open it.

        Returns None when the members could not be read at all — an unshared file, a
        revoked scope, a throttled call. None means "unknown" and keeps the file
        fail-closed; an empty AccessControl would instead assert that nobody may read it.
        A failure here never aborts the scan or drops the file.
        """
        if not self._mirror_permissions or not file_id:
            return None
        try:
            data = await self._post(
                "https://api.dropboxapi.com/2/sharing/list_file_members", {"file": file_id}
            )
        except Exception as e:
            self.logger.warning(f"Could not read sharing members for file {file_id}: {e}")
            return None
        return dropbox_members_to_access(data.get("users"), data.get("groups"))

    async def _download_file(
        self,
        file_entity: DropboxFileEntity,
        files: FileService,
    ) -> Optional[DropboxFileEntity]:
        """Download a file from Dropbox using the content API.

        Dropbox requires POST with Dropbox-API-Arg header for downloads.
        """
        dropbox_api_arg = json.dumps({"path": file_entity.path_lower})
        token = await self.auth.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": dropbox_api_arg,
        }

        response = await self.http_client.post(
            "https://content.dropboxapi.com/2/files/download",
            headers=headers,
        )

        if response.status_code == 401 and self.auth.supports_refresh:
            new_token = await self.auth.force_refresh()
            headers["Authorization"] = f"Bearer {new_token}"
            response = await self.http_client.post(
                "https://content.dropboxapi.com/2/files/download",
                headers=headers,
            )

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
            context=f"downloading {file_entity.name}",
        )

        content = response.content
        await files.save_bytes(
            entity=file_entity,
            content=content,
            filename_with_extension=file_entity.name,
            logger=self.logger,
        )

        if not file_entity.local_path:
            self.logger.warning(f"Save failed — no local path set for {file_entity.name}")
            return None

        self.logger.debug(f"Successfully downloaded file: {file_entity.name}")
        return file_entity

    async def _generate_file_entities(
        self, folder_breadcrumbs: List[Breadcrumb], folder_path: str, files: FileService
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate file entities within a given folder using the Dropbox API."""
        url = "https://api.dropboxapi.com/2/files/list_folder"
        continue_url = "https://api.dropboxapi.com/2/files/list_folder/continue"

        data = {
            "path": folder_path,
            "recursive": False,
            "include_deleted": False,
            "include_has_explicit_shared_members": True,
            "include_mounted_folders": True,
            "include_non_downloadable_files": True,
        }

        async for entry in self._get_paginated_entries(url, data, continue_url):
            if entry.get(".tag") != "file":
                continue
            async for file_entity in self._emit_file_entry(entry, folder_breadcrumbs, files):
                yield file_entity

    async def _emit_file_entry(
        self, entry: Dict, folder_breadcrumbs: List[Breadcrumb], files: FileService
    ) -> AsyncGenerator[BaseEntity, None]:
        """Turn one ``list_folder`` file entry into a downloaded entity."""
        if not entry.get("is_downloadable", True):
            self.logger.debug(
                f"Skipping non-downloadable file: {entry.get('path_display', 'unknown path')}"
            )
            return

        file_entity = self._create_file_entity(entry, folder_breadcrumbs)
        file_entity.access = await self._get_file_access(entry.get("id", ""))

        try:
            result = await self._download_file(file_entity, files)
            if result:
                yield result
        except FileSkippedException as e:
            self.logger.debug(f"Skipping file: {e.reason}")
        except SourceAuthError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            self.logger.warning(f"Skipping file {file_entity.name}: HTTP {e.response.status_code}")

    async def _process_folder_and_contents(
        self, folder_path: str, folder_breadcrumbs: List[Breadcrumb], files: FileService
    ) -> AsyncGenerator[BaseEntity, None]:
        """Process a folder recursively, yielding files and subfolders."""
        async for file_entity in self._generate_file_entities(
            folder_breadcrumbs, folder_path, files
        ):
            yield file_entity

        url = "https://api.dropboxapi.com/2/files/list_folder"
        continue_url = "https://api.dropboxapi.com/2/files/list_folder/continue"

        data = {
            "path": folder_path,
            "recursive": False,
            "include_deleted": False,
            "include_has_explicit_shared_members": True,
            "include_mounted_folders": True,
            "include_non_downloadable_files": True,
        }

        async for entry in self._get_paginated_entries(url, data, continue_url):
            if entry.get(".tag") == "folder":
                account_breadcrumb = folder_breadcrumbs[0] if folder_breadcrumbs else None
                folder_entity, subfolder_path = self._create_folder_entity(
                    entry, account_breadcrumb
                )
                yield folder_entity

                folder_breadcrumb = Breadcrumb(
                    entity_id=folder_entity.id,
                    name=folder_entity.name,
                    entity_type="DropboxFolderEntity",
                )
                new_breadcrumbs = folder_breadcrumbs + [folder_breadcrumb]

                async for entity in self._process_folder_and_contents(
                    subfolder_path, new_breadcrumbs, files
                ):
                    yield entity

    @staticmethod
    def _selected_paths(node_selections: list[NodeSelectionData] | None) -> List[str]:
        """The folder paths this connection was scoped to, in Dropbox's own form."""
        paths: List[str] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            path = str(metadata.get("path") or selection.source_node_id or "").strip().rstrip("/")
            if path and not path.startswith("/"):
                path = f"/{path}"
            if path not in paths:
                paths.append(path)
        return paths

    async def _generate_selected_subtree(
        self,
        account_breadcrumb: Breadcrumb,
        folder_path: str,
        files: FileService,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Yield everything under one selected folder in a single recursive listing.

        ``recursive=true`` is what keeps a root live: a matter folder opened under it
        tomorrow is picked up without anyone re-choosing the scope.
        """
        url = "https://api.dropboxapi.com/2/files/list_folder"
        continue_url = "https://api.dropboxapi.com/2/files/list_folder/continue"
        data = {
            "path": folder_path,
            "recursive": True,
            "include_deleted": False,
            "include_has_explicit_shared_members": True,
            "include_mounted_folders": True,
            "include_non_downloadable_files": True,
        }
        breadcrumbs = [account_breadcrumb]

        try:
            async for entry in self._get_paginated_entries(url, data, continue_url):
                tag = entry.get(".tag")
                if tag == "folder":
                    folder_entity, _path = self._create_folder_entity(entry, account_breadcrumb)
                    yield folder_entity
                elif tag == "file":
                    async for file_entity in self._emit_file_entry(entry, breadcrumbs, files):
                        yield file_entity
        except SourceAuthError:
            raise
        except Exception as e:
            # Never widen back to the whole account: a selected folder that vanished is
            # one root fewer, not a licence to index a partner's personal files.
            self.logger.warning(
                f"Selected folder {folder_path or '/'} could not be listed ({e}); "
                "skipping it — the remaining selected folders still sync"
            )

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """List the folders under a path so an operator can pick which subtrees to sync.

        ``None`` (or ``""``) lists the account root.
        """
        path = str(parent_node_id or "").strip().rstrip("/")
        if path and not path.startswith("/"):
            path = f"/{path}"

        url = "https://api.dropboxapi.com/2/files/list_folder"
        continue_url = "https://api.dropboxapi.com/2/files/list_folder/continue"
        data = {
            "path": path,
            "recursive": False,
            "include_deleted": False,
            "include_mounted_folders": True,
            "include_non_downloadable_files": True,
        }

        nodes: List[BrowseNode] = []
        async for entry in self._get_paginated_entries(url, data, continue_url):
            if entry.get(".tag") != "folder":
                continue
            folder_path = entry.get("path_lower", "")
            nodes.append(
                BrowseNode(
                    source_node_id=folder_path,
                    node_type="folder",
                    title=entry.get("name", folder_path),
                    # Dropbox does not report child counts, so every folder stays
                    # expandable rather than looking empty in the picker.
                    has_children=True,
                    node_metadata={
                        "path": folder_path,
                        "path_display": entry.get("path_display"),
                    },
                )
            )
        return nodes

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Recursively generate all entities from Dropbox."""
        assert files is not None, "FileService is required for Dropbox"

        selected_paths = self._selected_paths(node_selections)

        async for account_entity in self._generate_account_entities():
            yield account_entity

            account_breadcrumb = Breadcrumb(
                entity_id=account_entity.account_id,
                name=account_entity.display_name,
                entity_type="DropboxAccountEntity",
            )
            account_breadcrumbs = [account_breadcrumb]

            if selected_paths:
                self.logger.info(
                    f"Sync strategy: TARGETED ({len(selected_paths)} folder roots)"
                )
                for folder_path in selected_paths:
                    async for entity in self._generate_selected_subtree(
                        account_breadcrumb, folder_path, files
                    ):
                        yield entity
                continue

            async for file_entity in self._generate_file_entities(account_breadcrumbs, "", files):
                yield file_entity

            async for folder_entity in self._generate_folder_entities(account_breadcrumb):
                if (
                    self._exclude_path
                    and folder_entity.path_lower
                    and self._exclude_path in folder_entity.path_lower
                ):
                    self.logger.debug(f"Skipping excluded folder: {folder_entity.path_lower}")
                    continue

                yield folder_entity

                folder_breadcrumb = Breadcrumb(
                    entity_id=folder_entity.id,
                    name=folder_entity.name,
                    entity_type="DropboxFolderEntity",
                )
                folder_breadcrumbs = [account_breadcrumb, folder_breadcrumb]

                async for entity in self._process_folder_and_contents(
                    folder_entity.path_lower, folder_breadcrumbs, files
                ):
                    yield entity

    async def validate(self) -> None:
        """Verify Dropbox OAuth2 token by calling /users/get_current_account (POST, no body)."""
        await self._post("https://api.dropboxapi.com/2/users/get_current_account", None)
