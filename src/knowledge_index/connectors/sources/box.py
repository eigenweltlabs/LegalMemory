"""Box source implementation for syncing folders, files, comments, users, and collaborations."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

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
from knowledge_index.connectors.acl import box_collaborations_to_access
from knowledge_index.connectors.configs import BoxAuthConfig
from knowledge_index.connectors.configs import BoxConfig
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.box import (
    BoxCollaborationEntity,
    BoxCommentEntity,
    BoxFileEntity,
    BoxFolderEntity,
    BoxUserEntity,
)
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.types import AuthenticationMethod, OAuthType


# Hosts the firm's Box token may be sent to. A content request redirects to a
# pre-signed dl*.boxcloud.com URL, which is cross-origin and needs no credential.
BOX_AUTH_HOSTS = ("box.com",)


@source(
    name="Box",
    short_name="box",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    auth_config_class=BoxAuthConfig,
    config_class=BoxConfig,
    labels=["Storage"],
    supports_continuous=False,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class BoxSource(BaseSource):
    """Box source connector integrates with the Box API to extract and synchronize data.

    Connects to your Box account and syncs folders, files, comments, users, and collaborations.
    """

    API_BASE = "https://api.box.com/2.0"

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: BoxConfig,
    ) -> BoxSource:
        """Create a new Box source."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._folder_id = str(config.folder_id).strip() or "0"
        instance._mirror_permissions = config.mirror_permissions
        return instance

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict:
        """Make authenticated GET request to Box API with token refresh support."""
        token = await self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            new_token = await self.auth.force_refresh()
            headers["Authorization"] = f"Bearer {new_token}"
            response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code in (403, 404):
            self.logger.warning(f"Box API {response.status_code} for {url}")
            return {}

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    async def _get_current_user(self) -> Optional[Dict]:
        """Get information about the current authenticated user."""
        try:
            user_data = await self._get(f"{self.API_BASE}/users/me")
            return user_data
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Failed to get current user: {e}")
            return None

    async def _generate_user_entity(self, user_id: str) -> Optional[BoxUserEntity]:
        """Generate a user entity for a given user ID."""
        try:
            user_data = await self._get(f"{self.API_BASE}/users/{user_id}")

            if not user_data:
                return None

            return BoxUserEntity.from_api(user_data)
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.debug(f"Failed to get user {user_id}: {e}")
            return None

    async def _process_folder_items(
        self,
        folder_id: str,
        folder_breadcrumbs: List[Breadcrumb],
        files: FileService | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Process all items (files and subfolders) within a folder with pagination."""
        offset = 0
        limit = 1000
        item_fields = (
            "id,name,type,description,size,path_collection,created_at,modified_at,"
            "content_created_at,content_modified_at,created_by,modified_by,owned_by,"
            "parent,item_status,shared_link,tags,has_collaborations,permissions,"
            "etag,sequence_id,sha1,extension,version_number,comment_count,lock"
        )

        while True:
            items_data = await self._get(
                f"{self.API_BASE}/folders/{folder_id}/items",
                params={"fields": item_fields, "limit": limit, "offset": offset},
            )

            if not items_data or not items_data.get("entries"):
                break

            for item in items_data.get("entries", []):
                item_type = item.get("type")

                if item.get("item_status") == "trashed":
                    self.logger.debug(f"Skipping trashed {item_type}: {item.get('name')}")
                    continue

                if item_type == "folder":
                    async for entity in self._generate_folder_entities(
                        item["id"], folder_breadcrumbs, files
                    ):
                        yield entity

                elif item_type == "file":
                    async for entity in self._generate_file_entities(
                        item, folder_breadcrumbs, files
                    ):
                        yield entity

            total_count = items_data.get("total_count", 0)
            offset += limit
            if offset >= total_count:
                break

    async def _generate_folder_entities(
        self,
        folder_id: str,
        breadcrumbs: List[Breadcrumb],
        files: FileService | None = None,
        is_selected_root: bool = False,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate folder and file entities recursively for a folder."""
        folder_fields = (
            "id,name,description,size,path_collection,created_at,modified_at,"
            "content_created_at,content_modified_at,created_by,modified_by,"
            "owned_by,parent,item_status,shared_link,folder_upload_email,"
            "tags,has_collaborations,permissions,etag,sequence_id"
        )
        folder_data = await self._get(
            f"{self.API_BASE}/folders/{folder_id}",
            params={"fields": folder_fields},
        )

        if not folder_data:
            if is_selected_root:
                # ``_get`` turns 403/404 into an empty body, so a deleted folder or a
                # withdrawn grant lands here. Skip that root and keep going; widening
                # the scan back to folder "0" would sync the whole account behind an
                # operator who asked for one matter folder.
                self.logger.warning(
                    f"Selected folder {folder_id} could not be read (deleted or access "
                    "lost); skipping it — the remaining selected folders still sync"
                )
            return

        folder_entity = BoxFolderEntity.from_api(folder_data, breadcrumbs=breadcrumbs)
        yield folder_entity

        folder_breadcrumb = Breadcrumb(
            entity_id=folder_entity.folder_id,
            name=folder_entity.name,
            entity_type="BoxFolderEntity",
        )
        folder_breadcrumbs = [*breadcrumbs, folder_breadcrumb]

        if folder_data["id"] != "0":
            async for collab_entity in self._generate_collaboration_entities(
                folder_data["id"], "folder", folder_data.get("name", ""), folder_breadcrumbs
            ):
                yield collab_entity

        async for entity in self._process_folder_items(folder_id, folder_breadcrumbs, files):
            yield entity

    async def _get_file_access(self, file_id: str) -> Optional[AccessControl]:
        """Read a file's collaborations so it is retrievable by whoever can open it in Box.

        Returns None when the collaborations could not be read at all. ``_get`` already
        turns 403/404 into an empty body, so an inaccessible collaboration list also lands
        here as None. None means "unknown" and keeps the file fail-closed; an empty
        AccessControl would instead assert that nobody may read it. A failure here never
        aborts the scan or drops the file.
        """
        if not self._mirror_permissions:
            return None
        try:
            data = await self._get(
                f"{self.API_BASE}/files/{file_id}/collaborations",
                params={"fields": "id,role,accessible_by,status"},
            )
        except Exception as e:
            self.logger.warning(f"Could not read collaborations for file {file_id}: {e}")
            return None
        return box_collaborations_to_access(data.get("entries"))

    async def _maybe_download_file(
        self,
        entity: BoxFileEntity,
        files: FileService | None,
        is_box_note: bool,
        can_download: bool,
    ) -> None:
        """Attempt to download a file via FileService. Mutates entity.local_path in place.

        Box Notes and files without download permission are skipped silently.
        401 propagates (dead token). Other HTTP errors log a warning.
        """
        if is_box_note:
            self.logger.debug(f"Skipping Box Note (proprietary format): {entity.name}")
            return
        if not can_download:
            self.logger.debug(f"Skipping file without download permission: {entity.name}")
            return
        if not files:
            return
        try:
            await files.download_from_url(
                entity=entity,
                client=self.http_client,
                auth=self.auth,
                logger=self.logger,
                auth_hosts=BOX_AUTH_HOSTS,
            )
        except FileSkippedException as e:
            self.logger.debug(f"Skipping file: {e.reason}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            self.logger.warning(f"Failed to download {entity.name}: {e}")

    async def _generate_file_entities(
        self,
        file_data: Dict,
        breadcrumbs: List[Breadcrumb],
        files: FileService | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate file entity and related entities (comments, collaborations)."""
        parent = file_data.get("parent") or {}
        parent_folder_id = parent.get("id") if parent else ""
        parent_folder_name = parent.get("name") if parent else ""

        path_collection_data = file_data.get("path_collection") or {}
        path_entries = path_collection_data.get("entries") or []

        file_name = file_data.get("name", "")
        file_extension = file_data.get("extension", "").lower()
        mime_type = file_data.get("mime_type") or "application/octet-stream"
        size = file_data.get("size", 0)

        is_box_note = file_extension == "boxnote"

        if mime_type and "/" in mime_type:
            file_type = mime_type.split("/")[0]
        elif file_extension:
            file_type = file_extension
        else:
            file_type = "file"

        file_entity = BoxFileEntity(
            file_id=file_data["id"],
            breadcrumbs=breadcrumbs,
            name=file_name,
            created_at=file_data.get("created_at"),
            updated_at=file_data.get("modified_at"),
            url=f"{self.API_BASE}/files/{file_data['id']}/content",
            size=size,
            file_type=file_type,
            mime_type=mime_type,
            local_path=None,
            description=file_data.get("description"),
            parent_folder_id=parent_folder_id,
            parent_folder_name=parent_folder_name,
            path_collection=[
                {"id": entry.get("id"), "name": entry.get("name")} for entry in path_entries
            ],
            sha1=file_data.get("sha1"),
            extension=file_data.get("extension"),
            version_number=file_data.get("version_number"),
            comment_count=file_data.get("comment_count"),
            content_created_at=file_data.get("content_created_at"),
            content_modified_at=file_data.get("content_modified_at"),
            created_by=file_data.get("created_by"),
            modified_by=file_data.get("modified_by"),
            owned_by=file_data.get("owned_by"),
            item_status=file_data.get("item_status"),
            shared_link=file_data.get("shared_link"),
            tags=file_data.get("tags", []),
            has_collaborations=file_data.get("has_collaborations"),
            permissions=file_data.get("permissions"),
            lock=file_data.get("lock"),
            permalink_url=f"https://app.box.com/file/{file_data['id']}",
            etag=file_data.get("etag"),
            sequence_id=file_data.get("sequence_id"),
        )

        permissions = file_data.get("permissions") or {}
        can_download = permissions.get("can_download", False)

        file_entity.access = await self._get_file_access(file_data["id"])

        await self._maybe_download_file(file_entity, files, is_box_note, can_download)
        yield file_entity

        file_breadcrumb = Breadcrumb(
            entity_id=file_entity.file_id,
            name=file_entity.name,
            entity_type="BoxFileEntity",
        )
        file_breadcrumbs = [*breadcrumbs, file_breadcrumb]

        async for comment_entity in self._generate_comment_entities(
            file_data["id"], file_data.get("name", ""), file_breadcrumbs
        ):
            yield comment_entity

        async for collab_entity in self._generate_collaboration_entities(
            file_data["id"], "file", file_data.get("name", ""), file_breadcrumbs
        ):
            yield collab_entity

    async def _generate_comment_entities(
        self,
        file_id: str,
        file_name: str,
        breadcrumbs: List[Breadcrumb],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate comment entities for a file."""
        try:
            comment_fields = (
                "id,message,created_by,created_at,modified_at,is_reply_comment,tagged_message"
            )
            comments_data = await self._get(
                f"{self.API_BASE}/files/{file_id}/comments",
                params={"fields": comment_fields},
            )

            if not comments_data or not comments_data.get("entries"):
                return

            for comment in comments_data.get("entries", []):
                yield BoxCommentEntity.from_api(
                    comment,
                    file_id=file_id,
                    file_name=file_name,
                    breadcrumbs=breadcrumbs,
                )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.debug(f"Failed to get comments for file {file_id}: {e}")

    async def _generate_collaboration_entities(
        self,
        item_id: str,
        item_type: str,
        item_name: str,
        breadcrumbs: List[Breadcrumb],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate collaboration entities for a file or folder."""
        try:
            collab_fields = (
                "id,role,accessible_by,item,status,created_at,modified_at,created_by,"
                "expires_at,is_access_only,invite_email,acknowledged_at"
            )
            collabs_data = await self._get(
                f"{self.API_BASE}/{item_type}s/{item_id}/collaborations",
                params={"fields": collab_fields},
            )

            if not collabs_data or not collabs_data.get("entries"):
                return

            for collab in collabs_data.get("entries", []):
                yield BoxCollaborationEntity.from_api(
                    collab,
                    item_id=item_id,
                    item_type=item_type,
                    item_name=item_name,
                    breadcrumbs=breadcrumbs,
                )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.debug(f"Failed to get collaborations for {item_type} {item_id}: {e}")

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all entities from Box."""
        self.logger.debug("Starting Box sync")

        current_user = await self._get_current_user()
        if current_user:
            user_entity = await self._generate_user_entity(current_user["id"])
            if user_entity:
                self.logger.debug(f"Syncing Box for user: {current_user.get('name')}")
                yield user_entity

        selected_folder_ids = self._selected_folder_ids(node_selections)
        if selected_folder_ids:
            self.logger.info(f"Sync strategy: TARGETED ({len(selected_folder_ids)} folder roots)")

        for folder_id in selected_folder_ids or [self._folder_id]:
            self.logger.debug(f"Starting folder sync from folder ID: {folder_id}")
            async for entity in self._generate_folder_entities(
                folder_id, [], files, is_selected_root=bool(selected_folder_ids)
            ):
                yield entity

        self.logger.debug("Box sync completed")

    @staticmethod
    def _selected_folder_ids(
        node_selections: list[NodeSelectionData] | None,
    ) -> List[str]:
        """The Box folder ids of the subtrees this connection was scoped to."""
        folder_ids: List[str] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            folder_id = str(metadata.get("folder_id") or selection.source_node_id or "").strip()
            if folder_id and folder_id not in folder_ids:
                folder_ids.append(folder_id)
        return folder_ids

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """List the sub-folders of a folder so an operator can pick sync roots.

        ``None`` lists the account root, which Box calls folder ``"0"``. The existing
        recursive traversal is what makes a root live, so only folders are offered.
        """
        folder_id = str(parent_node_id or "0")
        nodes: List[BrowseNode] = []
        offset = 0
        limit = 1000

        while True:
            items_data = await self._get(
                f"{self.API_BASE}/folders/{folder_id}/items",
                params={"fields": "id,name,type,item_status", "limit": limit, "offset": offset},
            )
            entries = (items_data or {}).get("entries") or []
            for item in entries:
                if item.get("type") != "folder" or item.get("item_status") == "trashed":
                    continue
                nodes.append(
                    BrowseNode(
                        source_node_id=item["id"],
                        node_type="folder",
                        title=item.get("name", item["id"]),
                        # Box's item listing carries no child count, so every folder
                        # stays expandable rather than looking empty in the picker.
                        has_children=True,
                        node_metadata={"folder_id": item["id"]},
                    )
                )

            offset += limit
            if not entries or offset >= (items_data or {}).get("total_count", 0):
                break

        return nodes

    async def validate(self) -> None:
        """Validate credentials by pinging Box's /users/me endpoint."""
        await self._get(f"{self.API_BASE}/users/me")
