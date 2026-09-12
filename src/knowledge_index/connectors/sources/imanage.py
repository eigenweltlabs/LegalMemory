"""iManage Work source implementation using the Work API v2.

Retrieves a firm's iManage estate:
 - Workspaces (IManageWorkspaceEntity — in a law firm, the matter, and the sync root)
 - Folders (IManageFolderEntity containers, giving documents their breadcrumb)
 - Documents (IManageDocumentEntity files, staged for the fetch stage)

Incremental sync:
 - iManage has no delta feed. An incremental run searches each synced library for
   documents edited since the watermark, which reports edits and additions.
 - Security is the other half: a firm walls a matter by re-securing the *workspace*,
   and no document's ``edit_date`` moves when that happens. Every incremental run
   re-reads workspace security and diffs it against the cursor snapshot, so a
   re-secured workspace re-emits its documents at the policy interval.
 - Deletions have no tombstone; they are reconciled from the cursor's per-workspace id
   snapshot, with the periodic full scan as backstop.

Access graph generation:
 - iManage states security as ``default_security`` plus an ``acl`` of trustees, each
   with an access level. Only levels that confer read become viewers; ``no_access`` is
   the explicit denial an ethical wall is built from and is never inverted into a grant.
 - Crucially, iManage says on each object whether it *inherits*. That makes inheritance
   the source's own answer rather than this connector's guess: an inheriting document
   takes its container's mirrored access, and an overriding one is read on its own.
   Where an override cannot be read the document stays fail-closed rather than falling
   back to the container, because an override generally exists in order to be narrower.
 - Groups are expanded to their members so a grant matches a real caller.

Provenance and its limits: this connector was written against the iManage Work API
definition Microsoft publishes for its certified Power Platform connector
(microsoft/PowerPlatformConnectors, MIT). That definition is authoritative for the
operations and — unusually and usefully — for the security payload's schema, which it
declares in full. It describes Microsoft's own facade rather than the native REST
paths, so the *paths* used here are the native Work API v2 ones and are partly
inferred. Everything unverified is listed in the PR that introduced this file, and the
connector stays out of the admin UI until one live tenant sync confirms it.
"""

from datetime import UTC, datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.acl import (
    IMANAGE_INHERIT,
    imanage_security_to_access,
)
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.configs import IManageConfig
from knowledge_index.connectors.cursors.imanage import IManageCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.imanage import (
    IManageDocumentDeletionEntity,
    IManageDocumentEntity,
    IManageFolderEntity,
    IManageWorkspaceEntity,
)
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.errors import FileSkippedException, SourceAuthError
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

PAGE_LIMIT = 100
MAX_FOLDER_DEPTH = 32

# What the cursor stores for an object whose access could not be read. Distinct from an
# empty list, which is a real answer ("these named trustees, nobody else"), and from a
# public one. Without the distinction an unreadable workspace would look identical to a
# genuinely empty one and the diff would miss the moment it became readable again.
UNKNOWN_ACCESS = ["?unknown"]


def _access_key(access: Optional[AccessControl]) -> List[str]:
    """A comparable snapshot of one mirrored ACL.

    ``is_public`` has to be part of it. Storing only ``viewers`` made a public
    workspace's snapshot (empty list) differ from its own recomputed value on every
    run, so every public matter re-synced its entire contents every time.
    """
    if access is None:
        return list(UNKNOWN_ACCESS)
    viewers = sorted(set(access.viewers or []))
    if access.is_public:
        viewers.append("role:authenticated")
    return viewers


def _access_from_key(key: Optional[List[str]]) -> Optional[AccessControl]:
    """Rebuild a mirrored ACL from its snapshot, preserving unknown as unknown."""
    if not key or key == UNKNOWN_ACCESS:
        return None
    viewers = [viewer for viewer in key if viewer != "role:authenticated"]
    return AccessControl(viewers=viewers, is_public="role:authenticated" in key)


@source(
    name="iManage Work",
    short_name="imanage",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    auth_config_class=None,
    config_class=IManageConfig,
    labels=["Legal DMS", "Legal"],
    supports_continuous=True,
    cursor_class=IManageCursor,
    supports_access_control=True,
    supports_browse_tree=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class IManageSource(BaseSource):
    """iManage Work source connector: workspaces, folders and documents."""

    # One security call per workspace plus one per referenced group. A firm has
    # thousands of matters but a sync is normally scoped to a fraction of them, and
    # membership freshness is what bounds how long a re-walled matter stays wrong.
    cheap_memberships = True

    _api_base_url: str
    _customer_id: str
    _mirror_permissions: bool
    _read_document_security: bool
    _tracked_groups: Dict[str, str]

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: IManageConfig,
    ) -> "IManageSource":
        """Create a new iManage source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._api_base_url = config.api_base_url.rstrip("/")
        instance._customer_id = str(config.customer_id or "").strip()
        instance._mirror_permissions = config.mirror_permissions
        instance._read_document_security = config.read_document_security
        instance._tracked_groups = {}
        return instance

    @property
    def _auth_hosts(self) -> Tuple[str, ...]:
        """The only host the firm's bearer token may be sent to."""
        return (urlparse(self._api_base_url).netloc,)

    # --------------------------------------------------------------------------- http

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[Dict] = None) -> Any:
        """Make an authenticated GET request to the iManage Work API.

        A 403 or 404 becomes an empty body: an estate always contains workspaces the
        authorizing account cannot open, and one closed door must not end the scan.
        """
        token = await self.auth.get_token()
        headers = {"X-Auth-Token": token, "Accept": "application/json"}
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            self.logger.warning(f"Got 401 from iManage at {url}, refreshing token...")
            new_token = await self.auth.force_refresh()
            headers["X-Auth-Token"] = new_token
            response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code in (403, 404):
            self.logger.warning(f"iManage {response.status_code} for {url}")
            return {}

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    @property
    def _api(self) -> str:
        return f"{self._api_base_url}/work/api/v2"

    def _customer_path(self, suffix: str) -> str:
        return f"{self._api}/customers/{self._customer_id}{suffix}"

    @staticmethod
    def _payload(data: Any) -> Any:
        """iManage wraps every response in ``data``; unwrap it, tolerating a bare body."""
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def _records(self, data: Any) -> List[Dict]:
        payload = self._payload(data)
        if isinstance(payload, list):
            return [record for record in payload if isinstance(record, dict)]
        if isinstance(payload, dict):
            for key in ("results", "items", "documents", "folders", "workspaces"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    return [record for record in candidate if isinstance(record, dict)]
        return []

    async def _paginate(self, url: str, params: Optional[Dict] = None) -> AsyncGenerator[Dict, None]:
        """Yield records from a list endpoint, walking iManage's offset paging."""
        offset = 0
        while True:
            query = {**(params or {}), "limit": PAGE_LIMIT, "offset": offset}
            data = await self._get(url, params=query)
            records = self._records(data)
            for record in records:
                yield record
            if len(records) < PAGE_LIMIT:
                return
            offset += PAGE_LIMIT

    async def _resolve_customer_id(self) -> str:
        """The customer id every path is scoped by, read from the account if not set."""
        if self._customer_id:
            return self._customer_id
        data = await self._get(f"{self._api}/customers")
        for record in self._records(data):
            identifier = str(record.get("id") or "").strip()
            if identifier:
                self._customer_id = identifier
                return identifier
        raise SourceAuthError(
            "iManage did not report a customer id for this account; set it explicitly "
            "in the connection's settings"
        )

    # ------------------------------------------------------------------ access mirror

    async def _security(
        self,
        *,
        library_id: str,
        object_type: str,
        object_id: str,
        container_access: Optional[AccessControl] = None,
    ) -> Optional[AccessControl]:
        """Read and mirror one object's security.

        ``None`` when it could not be read: unknown, fail-closed, and reported as a
        capability gap rather than published to the firm.
        """
        if not self._mirror_permissions:
            return None
        url = (
            f"{self._customer_path(f'/libraries/{library_id}')}"
            f"/{object_type}/{object_id}/security"
        )
        try:
            data = await self._get(url)
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Could not read security for {object_type} {object_id}: {e}")
            return None
        payload = self._payload(data)
        if not isinstance(payload, dict) or not payload:
            return None
        access = imanage_security_to_access(payload, container_access=container_access)
        for entry in payload.get("acl") or []:
            if str(entry.get("type") or "").strip().lower() == "group":
                identifier = str(entry.get("id") or "").strip()
                if identifier:
                    self._tracked_groups[identifier] = library_id
        return access

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand tracked iManage groups into user memberships.

        Without these rows a grant to ``group:imanage:{id}`` matches no caller and a
        walled matter is invisible rather than protected.
        """
        if not self._mirror_permissions or not self._tracked_groups:
            return
        self.logger.info(f"Expanding {len(self._tracked_groups)} iManage groups")
        for group_id, library_id in sorted(self._tracked_groups.items()):
            url = f"{self._customer_path(f'/libraries/{library_id}')}/groups/{group_id}/members"
            try:
                data = await self._get(url)
            except SourceAuthError:
                raise
            except Exception as e:
                # Fail-closed: an unexpandable group grants nobody, and a later healthy
                # run restores its members.
                self.logger.warning(f"Could not expand iManage group {group_id}: {e}")
                continue
            for member in self._records(data):
                email = str(member.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                yield MembershipTuple(
                    member_id=email,
                    member_type="user",
                    group_id=f"imanage:{group_id.casefold()}",
                    group_name=str(member.get("group_name") or group_id),
                )

    # ------------------------------------------------------------------------- crawl

    @staticmethod
    def _selected_workspaces(
        node_selections: Optional[List[NodeSelectionData]],
    ) -> List[Tuple[str, str]]:
        """The (library, workspace) pairs this connection was scoped to."""
        selected: List[Tuple[str, str]] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            workspace_id = str(
                metadata.get("workspace_id") or selection.source_node_id or ""
            ).strip()
            library_id = str(metadata.get("library_id") or "").strip()
            if not library_id and "!" in workspace_id:
                library_id = workspace_id.partition("!")[0]
            if workspace_id and (library_id, workspace_id) not in selected:
                selected.append((library_id, workspace_id))
        return selected

    async def _libraries(self) -> List[str]:
        data = await self._get(self._customer_path("/libraries"))
        return [
            str(record["id"]) for record in self._records(data) if record.get("id")
        ]

    async def _workspaces(self, library_id: str) -> AsyncGenerator[Dict, None]:
        url = self._customer_path(f"/libraries/{library_id}/workspaces")
        async for record in self._paginate(url):
            yield record

    async def _walk_folder(
        self,
        folder_id: str,
        *,
        library_id: str,
        workspace_id: str,
        container_access: Optional[AccessControl],
        breadcrumbs: List[Breadcrumb],
        files: FileService | None,
        seen: set[str],
        document_ids: List[str],
        depth: int = 0,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Walk a folder's subfolders and documents depth-first."""
        if folder_id in seen or depth > MAX_FOLDER_DEPTH:
            return
        seen.add(folder_id)

        base = self._customer_path(f"/libraries/{library_id}/folders/{folder_id}")

        async for record in self._paginate(f"{base}/subfolders"):
            identifier = str(record.get("id") or "").strip()
            if not identifier or identifier in seen:
                continue
            folder = IManageFolderEntity.from_api(
                record, library_id=library_id, breadcrumbs=breadcrumbs
            )
            if folder is None:
                continue
            # A folder states its own security exactly as a document does, so a
            # restricted subfolder inside an open matter is honoured rather than
            # flattened to the workspace's audience.
            folder_access = await self._security(
                library_id=library_id,
                object_type="folders",
                object_id=identifier,
                container_access=container_access,
            )
            folder.access = folder_access
            yield folder

            child_breadcrumbs = [
                *breadcrumbs,
                Breadcrumb(
                    entity_id=folder.folder_id,
                    name=folder.name,
                    entity_type="IManageFolderEntity",
                ),
            ]
            async for entity in self._walk_folder(
                identifier,
                library_id=library_id,
                workspace_id=workspace_id,
                container_access=folder_access,
                breadcrumbs=child_breadcrumbs,
                files=files,
                seen=seen,
                document_ids=document_ids,
                depth=depth + 1,
            ):
                yield entity

        async for record in self._paginate(f"{base}/documents"):
            entity = await self._document_entity(
                record,
                library_id=library_id,
                folder_id=folder_id,
                container_access=container_access,
                breadcrumbs=breadcrumbs,
                files=files,
            )
            if entity is not None:
                document_ids.append(entity.document_id)
                yield entity

    async def _document_access(
        self, record: Dict, library_id: str, container_access: Optional[AccessControl]
    ) -> Optional[AccessControl]:
        """The mirrored ACL for one document.

        iManage states whether the document inherits. That is the source's own answer,
        so an inheriting document takes its container's access without a second call.
        An overriding document is read on its own; if that read is switched off or
        fails, it stays fail-closed rather than inheriting, because an override
        generally exists in order to be narrower than the container.
        """
        if not self._mirror_permissions:
            return None
        default_security = str(record.get("default_security") or "").strip().lower()
        if default_security == IMANAGE_INHERIT:
            return container_access
        if not self._read_document_security:
            return None
        identifier = str(record.get("id") or "").strip()
        if not identifier:
            return None
        return await self._security(
            library_id=library_id,
            object_type="documents",
            object_id=identifier,
            container_access=container_access,
        )

    async def _document_entity(
        self,
        record: Dict,
        *,
        library_id: str,
        folder_id: Optional[str],
        container_access: Optional[AccessControl],
        breadcrumbs: List[Breadcrumb],
        files: FileService | None,
    ) -> Optional[IManageDocumentEntity]:
        """Build one document entity, mirror its access, and stage its bytes."""
        entity = IManageDocumentEntity.from_api(
            record,
            api_base_url=self._api_base_url,
            customer_id=self._customer_id,
            library_id=library_id,
            folder_id=folder_id,
            breadcrumbs=breadcrumbs,
        )
        if entity is None:
            return None
        entity.access = await self._document_access(record, library_id, container_access)

        if files:
            try:
                await files.download_from_url(
                    entity=entity,
                    client=self.http_client,
                    auth=self.auth,
                    logger=self.logger,
                    auth_hosts=self._auth_hosts,
                )
                if not entity.local_path:
                    self.logger.warning(f"Download produced no local path for {entity.name}")
                    return None
            except FileSkippedException as e:
                self.logger.debug(f"Skipping document {entity.name}: {e.reason}")
                return None
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise
                self.logger.warning(
                    f"HTTP {e.response.status_code} downloading {entity.name}: {e}"
                )
                return None
        return entity

    # -------------------------------------------------------------------------- sync

    def _should_do_full_sync(self, cursor: SyncCursor | None) -> Tuple[bool, str]:
        cursor_data = cursor.data if cursor else {}
        if not cursor_data:
            return True, "no cursor data (first sync)"
        schema = IManageCursor(**cursor_data)
        if schema.needs_full_sync():
            return True, "full_sync_required flag set or no watermark"
        if schema.needs_periodic_full_sync():
            return True, "periodic full sync needed (>7 days since last)"
        return False, "incremental sync (valid watermark)"

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all iManage entities using full or incremental sync.

        Tracked groups are deliberately not seeded from the cursor: every run re-reads
        the security of every workspace it syncs, so the set rebuilds from the source.
        Restoring the previous set would keep expanding a group whose access the firm
        has just revoked.
        """
        await self._resolve_customer_id()
        selected = self._selected_workspaces(node_selections)
        is_full, reason = self._should_do_full_sync(cursor)
        scope_label = f"TARGETED ({len(selected)} workspaces) " if selected else ""
        self.logger.info(
            f"Sync strategy: {scope_label}{'FULL' if is_full else 'INCREMENTAL'} ({reason})"
        )

        if is_full:
            async for entity in self._full_sync(cursor, files, selected):
                yield entity
        else:
            async for entity in self._incremental_sync(cursor, files, selected):
                yield entity

        if cursor:
            cursor.update(tracked_groups=dict(self._tracked_groups))

    async def _workspace_records(
        self, selected: List[Tuple[str, str]]
    ) -> AsyncGenerator[Tuple[str, Dict], None]:
        """(library, workspace profile) pairs for the whole estate or the selection."""
        if selected:
            for library_id, workspace_id in selected:
                library = library_id or workspace_id.partition("!")[0]
                url = self._customer_path(f"/libraries/{library}/workspaces/{workspace_id}")
                try:
                    data = await self._get(url)
                except SourceAuthError:
                    raise
                except Exception as e:
                    # Never widen: a selected workspace that vanished or was walled
                    # away costs that one root, not the run.
                    self.logger.warning(
                        f"Selected workspace {workspace_id} could not be read ({e}); "
                        "skipping it — the remaining selected workspaces still sync"
                    )
                    continue
                payload = self._payload(data)
                if isinstance(payload, dict) and payload.get("id"):
                    yield library, payload
            return

        for library_id in await self._libraries():
            async for record in self._workspaces(library_id):
                yield library_id, record

    async def _sync_workspace(
        self,
        library_id: str,
        record: Dict,
        files: FileService | None,
    ) -> AsyncGenerator[Tuple[BaseEntity, List[str], List[str]], None]:
        """Yield a workspace's entities along with its viewers and document ids."""
        workspace = IManageWorkspaceEntity.from_api(record, library_id=library_id)
        if workspace is None:
            return
        access = await self._security(
            library_id=library_id,
            object_type="workspaces",
            object_id=workspace.workspace_id,
        )
        workspace.access = access
        viewers = _access_key(access)
        document_ids: List[str] = []
        yield workspace, viewers, document_ids

        breadcrumbs = [
            Breadcrumb(
                entity_id=workspace.workspace_id,
                name=workspace.name,
                entity_type="IManageWorkspaceEntity",
            )
        ]
        async for entity in self._walk_folder(
            workspace.workspace_id,
            library_id=library_id,
            workspace_id=workspace.workspace_id,
            container_access=access,
            breadcrumbs=breadcrumbs,
            files=files,
            seen=set(),
            document_ids=document_ids,
        ):
            yield entity, viewers, document_ids

    async def _full_sync(
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected: List[Tuple[str, str]],
    ) -> AsyncGenerator[BaseEntity, None]:
        watermark = datetime.now(UTC).isoformat()
        workspace_acls: Dict[str, List[str]] = {}
        workspace_documents: Dict[str, List[str]] = {}
        entity_count = 0

        async for library_id, record in self._workspace_records(selected):
            workspace_id = str(record.get("id") or "").strip()
            if not workspace_id:
                continue
            async for entity, viewers, document_ids in self._sync_workspace(
                library_id, record, files
            ):
                workspace_acls[workspace_id] = viewers
                workspace_documents[workspace_id] = document_ids
                yield entity
                entity_count += 1

        if cursor:
            schema = IManageCursor(**cursor.data)
            schema.edited_since = watermark
            schema.full_sync_required = False
            schema.last_full_sync_timestamp = datetime.now(UTC).isoformat()
            schema.last_entity_changes_count = entity_count
            schema.workspace_acls = workspace_acls
            schema.workspace_documents = workspace_documents
            cursor.update(**schema.model_dump())

        self.logger.info(f"Full sync complete: {entity_count} entities")

    async def _incremental_sync(  # noqa: C901
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected: List[Tuple[str, str]],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Drain edited documents — after diffing workspace security."""
        cursor_data = cursor.data if cursor else {}
        schema = IManageCursor(**cursor_data)
        if not schema.edited_since:
            async for entity in self._full_sync(cursor, files, selected):
                yield entity
            return

        watermark = datetime.now(UTC).isoformat()
        previous_acls = {str(k): list(v or []) for k, v in (schema.workspace_acls or {}).items()}
        workspace_documents = {
            str(k): [str(item) for item in v]
            for k, v in (schema.workspace_documents or {}).items()
        }
        current_acls: Dict[str, List[str]] = {}
        changes = 0
        emitted: set[str] = set()

        try:
            records = [pair async for pair in self._workspace_records(selected)]
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Workspace listing failed: {e}")
            if cursor:
                cursor.update(full_sync_required=True)
            return

        if not records and previous_acls:
            # An empty listing where the last run saw workspaces is far more likely to
            # be a withdrawn grant or an outage than a firm deleting its estate — and
            # ``_get`` reports 403 and 404 as an empty body, so both arrive here
            # looking identical. Falling through would take the deletion path and empty
            # the index.
            self.logger.warning(
                "Workspace listing came back empty but the previous run saw "
                f"{len(previous_acls)}; treating it as unreadable rather than deleted"
            )
            if cursor:
                cursor.update(full_sync_required=True)
            return

        libraries: set[str] = set()
        for library_id, record in records:
            workspace_id = str(record.get("id") or "").strip()
            if not workspace_id:
                continue
            libraries.add(library_id)

            access = await self._security(
                library_id=library_id, object_type="workspaces", object_id=workspace_id
            )
            viewers = _access_key(access)
            current_acls[workspace_id] = viewers

            if previous_acls.get(workspace_id) == viewers:
                continue

            # Re-secured: re-emit the whole workspace so every document carries the new
            # audience. Nothing in the document feed would have reported this.
            self.logger.info(f"Workspace {workspace_id} security changed; re-emitting it")
            async for entity, _viewers, document_ids in self._sync_workspace(
                library_id, record, files
            ):
                workspace_documents[workspace_id] = document_ids
                emitted.add(getattr(entity, "document_id", ""))
                yield entity
                changes += 1

        for library_id in sorted(libraries):
            async for entity in self._edited_documents(
                library_id, schema.edited_since, current_acls, files, emitted
            ):
                yield entity
                changes += 1

        for workspace_id in sorted(set(previous_acls) - set(current_acls)):
            for document_id in workspace_documents.pop(workspace_id, []):
                yield IManageDocumentDeletionEntity(
                    document_id=document_id, deletion_status="removed", breadcrumbs=[]
                )
                changes += 1

        if cursor:
            current = IManageCursor(**cursor.data)
            current.edited_since = watermark
            current.last_entity_changes_count = changes
            current.workspace_acls = current_acls
            current.workspace_documents = {
                workspace_id: ids
                for workspace_id, ids in workspace_documents.items()
                if workspace_id in current_acls
            }
            cursor.update(**current.model_dump())

        self.logger.info(f"Incremental sync complete: {changes} changes processed")

    async def _edited_documents(
        self,
        library_id: str,
        edited_since: str,
        current_acls: Dict[str, List[str]],
        files: FileService | None,
        emitted: set[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Documents in one library edited since the watermark."""
        url = self._customer_path(f"/libraries/{library_id}/documents/search")
        try:
            data = await self._get(url, params={"edit_date_from": edited_since[:10]})
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Edited-document search failed for {library_id}: {e}")
            return

        for record in self._records(data):
            document_id = str(record.get("id") or "").strip()
            if not document_id or document_id in emitted:
                continue
            workspace_id = str(record.get("workspace_id") or "").strip()
            # A document whose workspace this run could not read keeps unknown access
            # rather than the last audience we happened to remember.
            container_access = _access_from_key(current_acls.get(workspace_id))
            breadcrumbs = (
                [
                    Breadcrumb(
                        entity_id=workspace_id,
                        name=str(record.get("workspace_name") or workspace_id),
                        entity_type="IManageWorkspaceEntity",
                    )
                ]
                if workspace_id
                else []
            )
            entity = await self._document_entity(
                record,
                library_id=library_id,
                folder_id=None,
                container_access=container_access,
                breadcrumbs=breadcrumbs,
                files=files,
            )
            if entity is not None:
                yield entity

    # ------------------------------------------------------------------------ browse

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """List libraries, then their workspaces, so an operator can pick matters.

        Two levels because an estate has a handful of libraries and thousands of
        matters; offering every workspace at once is a picker nobody can use.
        """
        await self._resolve_customer_id()
        if not parent_node_id:
            return [
                BrowseNode(
                    source_node_id=f"library:{library_id}",
                    node_type="folder",
                    title=library_id,
                    has_children=True,
                    node_metadata={"library_id": library_id},
                )
                for library_id in await self._libraries()
            ]

        _kind, metadata = self.parse_browse_node_id(parent_node_id)
        library_id = str(metadata.get("library_id") or "")
        if not library_id:
            return []
        nodes: List[BrowseNode] = []
        async for record in self._workspaces(library_id):
            identifier = str(record.get("id") or "").strip()
            if not identifier:
                continue
            name = str(record.get("name") or identifier)
            description = record.get("description")
            nodes.append(
                BrowseNode(
                    source_node_id=identifier,
                    node_type="folder",
                    title=f"{name} — {description}" if description else name,
                    has_children=False,
                    node_metadata={"workspace_id": identifier, "library_id": library_id},
                )
            )
        return nodes

    def parse_browse_node_id(self, node_id: str) -> tuple:
        """Decode a browse id: ``library:NAME`` for a library, else a workspace id."""
        if node_id.startswith("library:"):
            return "folder", {"library_id": node_id.partition(":")[2]}
        return "folder", {
            "workspace_id": node_id,
            "library_id": node_id.partition("!")[0] if "!" in node_id else "",
        }

    async def validate(self) -> None:
        """Validate credentials by asking iManage which libraries the account can see."""
        customer_id = await self._resolve_customer_id()
        await self._get(f"{self._api}/customers/{customer_id}/libraries")
