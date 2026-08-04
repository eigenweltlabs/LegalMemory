"""NetDocuments source implementation using the NetDocuments REST API.

Retrieves a firm's NetDocuments estate:
 - Cabinets (NetDocumentsCabinetEntity, the security boundary and the sync root)
 - Workspaces and folders (NetDocumentsFolderEntity containers, giving documents their
   breadcrumb — which for a law firm is the matter a paragraph belongs to)
 - Documents (NetDocumentsDocumentEntity files, staged for the fetch stage)

Incremental sync:
 - NetDocuments has no delta feed. It has a cabinet search that takes the same query
   language as the web interface, so an incremental run asks each synced cabinet for
   documents modified since the watermark. That reports edits and additions.
 - Deletions have no tombstone: a deleted document simply stops matching. They are
   reconciled from the cursor's per-container id snapshot, and the periodic full scan
   is the backstop for a container that was itself removed.

Access graph generation:
 - NetDocuments secures content by cabinet and workspace membership: a group holds
   view / edit / share / administer rights, or an explicit no-access row that is how a
   firm builds an ethical wall. Containers mirror that membership; the groups are
   expanded to their members so a grant matches a real caller.
 - A document may carry an access list narrower than its container. The connector reads
   that list from the document profile when it is present. Where it is not, the
   document stays fail-closed rather than inheriting the container — inheriting would
   publish precisely the overrides that exist in order to be narrower. An operator who
   knows their repository does not use document overrides can opt into inheritance.

Provenance and its limits: this connector was written against the NetDocuments API
definition Microsoft publishes for its certified Power Platform connector
(microsoft/PowerPlatformConnectors, MIT). That spec is authoritative for paths,
parameters and OAuth scopes, and its OAuth endpoints and regional hosts were verified
live. It defines almost no response bodies, so the *shapes* read out of each payload
here are inferred and are marked as such in the PR that introduced this file. They need
one live repository sync to confirm before this connector is offered in the admin UI.
"""

from datetime import UTC, datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.acl import (
    netdocuments_document_acl_to_access,
    netdocuments_membership_to_access,
)
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.configs import NetDocumentsConfig
from knowledge_index.connectors.cursors.netdocuments import NetDocumentsCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.netdocuments import (
    NetDocumentsCabinetEntity,
    NetDocumentsDocumentDeletionEntity,
    NetDocumentsDocumentEntity,
    NetDocumentsFolderEntity,
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

# The container listing caps at 500 per page; ask for the cap so a matter workspace with
# a few thousand documents costs pages rather than round trips.
PAGE_LIMIT = 500

# Attribute values requested from a container listing. NetDocuments returns a thin
# record without a select, and a second profile call per document would multiply the
# request count by the size of the estate.
CONTAINER_SELECT = "id,name,type,extension,size,version,created,modified,author,client,matter"

# Container kinds the listing can return. Only these are walked; a document is a leaf.
CONTAINER_TYPES = frozenset({"workspace", "folder", "filter", "container", "cabinet"})


@source(
    name="NetDocuments",
    short_name="netdocuments",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    auth_config_class=None,
    config_class=NetDocumentsConfig,
    labels=["Legal DMS", "Legal"],
    supports_continuous=True,
    cursor_class=NetDocumentsCursor,
    supports_access_control=True,
    supports_browse_tree=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class NetDocumentsSource(BaseSource):
    """NetDocuments source connector: cabinets, workspaces and documents."""

    # One membership call per synced cabinet plus one per referenced group. A firm has
    # tens of cabinets, not thousands, so memberships can refresh on every sync and a
    # wall change lands at the policy interval.
    cheap_memberships = True

    _api_base_url: str
    _mirror_permissions: bool
    _inherit_container_access: bool
    _tracked_groups: Dict[str, str]

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: NetDocumentsConfig,
    ) -> "NetDocumentsSource":
        """Create a new NetDocuments source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._api_base_url = config.api_base_url.rstrip("/")
        instance._mirror_permissions = config.mirror_permissions
        instance._inherit_container_access = config.inherit_container_access_for_documents
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
        """Make an authenticated GET request to the NetDocuments API.

        A 403 or 404 becomes an empty body rather than an exception: a repository has
        cabinets the authorizing account cannot open, and one closed door must not end
        the scan. Callers that need to tell "empty" from "unreadable" apart for a
        permission decision do not use this path.
        """
        token = await self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            self.logger.warning(f"Got 401 from NetDocuments at {url}, refreshing token...")
            new_token = await self.auth.force_refresh()
            headers["Authorization"] = f"Bearer {new_token}"
            response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code in (403, 404):
            self.logger.warning(f"NetDocuments {response.status_code} for {url}")
            return {}

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    async def _list_container(self, container_id: str) -> AsyncGenerator[Dict, None]:
        """Yield the entries of one container, following ``skiptoken`` pagination."""
        params: Optional[Dict] = {"select": CONTAINER_SELECT, "top": PAGE_LIMIT}
        url = f"{self._api_base_url}/v2/container/{container_id}"
        while True:
            data = await self._get(url, params=params)
            if not isinstance(data, dict):
                return
            for record in self._entries(data):
                yield record
            skiptoken = data.get("skiptoken") or data.get("nextSkipToken")
            if not skiptoken:
                return
            params = {"select": CONTAINER_SELECT, "top": PAGE_LIMIT, "skiptoken": skiptoken}

    @staticmethod
    def _entries(data: Any) -> List[Dict]:
        """The records out of a listing payload, whatever it wrapped them in.

        The published spec documents no response body for these calls, so the wrapper
        key is not something this connector can assert. Reading the plausible ones and
        falling back to a bare list keeps a shape difference from silently producing an
        empty, and therefore invisible, sync.
        """
        if isinstance(data, list):
            return [record for record in data if isinstance(record, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("standardList", "results", "items", "entries", "value", "documents"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return [record for record in candidate if isinstance(record, dict)]
        return []

    # ------------------------------------------------------------------ access mirror

    async def _cabinet_access(self, cabinet_id: str) -> Optional[AccessControl]:
        """Mirror one cabinet's group membership.

        ``None`` when the membership could not be read: unknown, fail-closed, and
        reported as a capability gap rather than published to the firm.
        """
        if not self._mirror_permissions:
            return None
        try:
            data = await self._get(f"{self._api_base_url}/v1/cabinet/{cabinet_id}/membership")
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Could not read membership for cabinet {cabinet_id}: {e}")
            return None
        entries = self._entries(data)
        if not entries:
            return None
        access = netdocuments_membership_to_access(entries)
        # Only groups that ended up as viewers are worth expanding. A row denied with
        # "N" grants nobody, so fetching its members would cost a call per wall and,
        # on a repository where the denied group is not readable, log a failure for
        # something that was never a grant.
        granted = {principal.rpartition(":")[2] for principal in (access.viewers if access else [])}
        for entry in entries:
            identifier = str(entry.get("id") or entry.get("groupId") or "").strip()
            if identifier.casefold() in granted:
                self._tracked_groups[identifier] = str(entry.get("name") or "")
        return access

    def _document_access(
        self, record: Dict, container_access: Optional[AccessControl]
    ) -> Optional[AccessControl]:
        """The mirrored ACL for one document.

        A per-document access list wins when the profile carries one. Where it does
        not, the document stays unknown — fail-closed — unless the operator has
        explicitly accepted container inheritance for this repository.
        """
        if not self._mirror_permissions:
            return None
        for key in ("acl", "accessList", "security", "trustees"):
            candidate = record.get(key)
            if isinstance(candidate, list):
                explicit = netdocuments_document_acl_to_access(candidate)
                if explicit is not None:
                    return explicit
        if self._inherit_container_access:
            return container_access
        return None

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand tracked NetDocuments groups into user memberships.

        Without these rows a grant to ``group:netdocuments:{id}`` matches no caller and
        a cabinet's documents are invisible rather than protected.
        """
        if not self._mirror_permissions or not self._tracked_groups:
            return
        self.logger.info(f"Expanding {len(self._tracked_groups)} NetDocuments groups")
        for group_id in sorted(self._tracked_groups):
            try:
                data = await self._get(f"{self._api_base_url}/v1/Group/{group_id}/members")
            except SourceAuthError:
                raise
            except Exception as e:
                # Fail-closed: an unexpandable group grants nobody, and a later healthy
                # run restores its members.
                self.logger.warning(f"Could not expand NetDocuments group {group_id}: {e}")
                continue
            group_name = self._tracked_groups[group_id] or group_id
            for member in self._entries(data):
                email = str(
                    member.get("email") or member.get("userId") or member.get("id") or ""
                ).strip().lower()
                if not email or "@" not in email:
                    continue
                yield MembershipTuple(
                    member_id=email,
                    member_type="user",
                    group_id=f"netdocuments:{group_id}",
                    group_name=group_name,
                )

    # ------------------------------------------------------------------------- crawl

    @staticmethod
    def _selected_cabinet_ids(
        node_selections: Optional[List[NodeSelectionData]],
    ) -> List[str]:
        """The cabinet ids this connection was scoped to."""
        cabinet_ids: List[str] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            cabinet_id = str(
                metadata.get("cabinet_id") or selection.source_node_id or ""
            ).strip()
            if cabinet_id and cabinet_id not in cabinet_ids:
                cabinet_ids.append(cabinet_id)
        return cabinet_ids

    async def _fetch_cabinets(self) -> List[Dict]:
        """The cabinets the authorizing account can open."""
        data = await self._get(f"{self._api_base_url}/v1/User/cabinets")
        return self._entries(data)

    async def _walk_container(
        self,
        container_id: str,
        *,
        cabinet_id: str,
        container_access: Optional[AccessControl],
        breadcrumbs: List[Breadcrumb],
        files: FileService | None,
        seen: set[str],
        document_ids: Dict[str, List[str]],
        depth: int = 0,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Walk one container, yielding its folders and documents depth-first.

        ``seen`` guards against a repository where a saved filter points back up its own
        tree: NetDocuments containers are a graph, not strictly a tree, and following a
        cycle would loop until the sync times out.
        """
        if container_id in seen or depth > 32:
            return
        seen.add(container_id)

        async for record in self._list_container(container_id):
            record_type = str(record.get("type") or "").strip().lower()
            identifier = str(record.get("id") or record.get("envId") or "").strip()
            if not identifier:
                continue

            if record_type in CONTAINER_TYPES:
                if identifier in seen:
                    # Already emitted on another branch of this cabinet, or pointing
                    # back at an ancestor. NetDocuments containers are a graph rather
                    # than a tree, so the same folder legitimately appears twice; only
                    # the first occurrence is the one to index.
                    continue
                folder = NetDocumentsFolderEntity.from_api(
                    record,
                    cabinet_id=cabinet_id,
                    parent_id=container_id,
                    breadcrumbs=breadcrumbs,
                )
                if folder is None:
                    continue
                folder.access = container_access
                yield folder
                child_breadcrumbs = [
                    *breadcrumbs,
                    Breadcrumb(
                        entity_id=folder.folder_id,
                        name=folder.name,
                        entity_type="NetDocumentsFolderEntity",
                    ),
                ]
                async for entity in self._walk_container(
                    folder.folder_id,
                    cabinet_id=cabinet_id,
                    container_access=container_access,
                    breadcrumbs=child_breadcrumbs,
                    files=files,
                    seen=seen,
                    document_ids=document_ids,
                    depth=depth + 1,
                ):
                    yield entity
                continue

            entity = await self._document_entity(
                record,
                cabinet_id=cabinet_id,
                container_id=container_id,
                container_access=container_access,
                breadcrumbs=breadcrumbs,
                files=files,
            )
            if entity is not None:
                document_ids.setdefault(container_id, []).append(entity.document_id)
                yield entity

    async def _document_entity(
        self,
        record: Dict,
        *,
        cabinet_id: str,
        container_id: str,
        container_access: Optional[AccessControl],
        breadcrumbs: List[Breadcrumb],
        files: FileService | None,
    ) -> Optional[NetDocumentsDocumentEntity]:
        """Build one document entity, mirror its access, and stage its bytes."""
        entity = NetDocumentsDocumentEntity.from_api(
            record,
            api_base_url=self._api_base_url,
            cabinet_id=cabinet_id,
            folder_id=container_id,
            breadcrumbs=breadcrumbs,
        )
        if entity is None:
            return None
        entity.access = self._document_access(record, container_access)

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
        schema = NetDocumentsCursor(**cursor_data)
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
        """Generate all NetDocuments entities using full or incremental sync.

        Tracked groups are deliberately *not* seeded from the cursor. Every run — full
        or incremental — re-reads the membership of every cabinet it syncs, so the set
        rebuilds itself from the source each time. Carrying the previous run's set
        forward would keep expanding a group whose rights the firm has just revoked,
        which is a stale membership list for a wall that no longer exists.
        """
        selected = self._selected_cabinet_ids(node_selections)
        is_full, reason = self._should_do_full_sync(cursor)
        scope_label = f"TARGETED ({len(selected)} cabinets) " if selected else ""
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

    async def _full_sync(
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected: List[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        # Minted before the crawl so a document changed while the crawl runs is replayed
        # by the first incremental drain instead of lost until the periodic full scan.
        watermark = datetime.now(UTC).isoformat()

        cabinet_acls: Dict[str, List[str]] = {}
        container_documents: Dict[str, List[str]] = {}
        entity_count = 0

        for record in await self._fetch_cabinets():
            cabinet_id = str(record.get("id") or record.get("envId") or "").strip()
            if not cabinet_id:
                continue
            if selected and cabinet_id not in selected:
                continue

            access = await self._cabinet_access(cabinet_id)
            cabinet = NetDocumentsCabinetEntity.from_api(record)
            if cabinet is None:
                continue
            cabinet.access = access
            cabinet_acls[cabinet_id] = list(access.viewers) if access else []
            yield cabinet
            entity_count += 1

            breadcrumbs = [
                Breadcrumb(
                    entity_id=cabinet.cabinet_id,
                    name=cabinet.name,
                    entity_type="NetDocumentsCabinetEntity",
                )
            ]
            async for entity in self._walk_container(
                cabinet_id,
                cabinet_id=cabinet_id,
                container_access=access,
                breadcrumbs=breadcrumbs,
                files=files,
                seen=set(),
                document_ids=container_documents,
            ):
                yield entity
                entity_count += 1

        if cursor:
            schema = NetDocumentsCursor(**cursor.data)
            schema.modified_since = watermark
            schema.full_sync_required = False
            schema.last_full_sync_timestamp = datetime.now(UTC).isoformat()
            schema.last_entity_changes_count = entity_count
            schema.cabinet_acls = cabinet_acls
            schema.container_documents = container_documents
            cursor.update(**schema.model_dump())

        self.logger.info(f"Full sync complete: {entity_count} entities")

    async def _incremental_sync(
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected: List[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Drain modified documents per cabinet — and diff the cabinet access first.

        A wall in NetDocuments is built by changing a *cabinet's* membership, and no
        document timestamp moves when that happens. Cabinet membership is one call per
        cabinet, so every incremental run re-reads it and diffs against the cursor's
        snapshot: a cabinet whose access changed re-emits its documents. Access changes
        therefore land at the policy interval, with the periodic full scan as backstop.
        """
        cursor_data = cursor.data if cursor else {}
        schema = NetDocumentsCursor(**cursor_data)
        if not schema.modified_since:
            async for entity in self._full_sync(cursor, files, selected):
                yield entity
            return

        watermark = datetime.now(UTC).isoformat()
        previous_acls = {str(k): list(v or []) for k, v in (schema.cabinet_acls or {}).items()}
        container_documents = {
            str(k): [str(item) for item in v]
            for k, v in (schema.container_documents or {}).items()
        }
        changes = 0
        current_acls: Dict[str, List[str]] = {}

        try:
            cabinets = await self._fetch_cabinets()
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Cabinet listing failed: {e}")
            if cursor:
                cursor.update(full_sync_required=True)
            return

        if not cabinets and previous_acls:
            # An empty listing where the last run saw cabinets is far more likely to be
            # a withdrawn grant or an outage than a firm deleting its whole estate —
            # and ``_get`` reports 403 and 404 as an empty body, so both arrive here
            # looking like "no cabinets". Falling through would take the deletion path
            # below and empty the index. Ask for a full sync instead.
            self.logger.warning(
                "Cabinet listing came back empty but the previous run saw "
                f"{len(previous_acls)}; treating it as unreadable rather than deleted"
            )
            if cursor:
                cursor.update(full_sync_required=True)
            return

        for record in cabinets:
            cabinet_id = str(record.get("id") or record.get("envId") or "").strip()
            if not cabinet_id or (selected and cabinet_id not in selected):
                continue

            access = await self._cabinet_access(cabinet_id)
            viewers = list(access.viewers) if access else []
            current_acls[cabinet_id] = viewers

            if previous_acls.get(cabinet_id) != viewers:
                # Re-permissioned: re-emit the whole cabinet so every document carries
                # the new access. Cheaper than being wrong until the next full scan.
                self.logger.info(f"Cabinet {cabinet_id} access changed; re-emitting it")
                cabinet = NetDocumentsCabinetEntity.from_api(record)
                if cabinet is not None:
                    cabinet.access = access
                    yield cabinet
                    changes += 1
                    breadcrumbs = [
                        Breadcrumb(
                            entity_id=cabinet.cabinet_id,
                            name=cabinet.name,
                            entity_type="NetDocumentsCabinetEntity",
                        )
                    ]
                    async for entity in self._walk_container(
                        cabinet_id,
                        cabinet_id=cabinet_id,
                        container_access=access,
                        breadcrumbs=breadcrumbs,
                        files=files,
                        seen=set(),
                        document_ids=container_documents,
                    ):
                        yield entity
                        changes += 1
                continue

            async for entity in self._modified_documents(
                cabinet_id, schema.modified_since, access, files
            ):
                yield entity
                changes += 1

        # A cabinet that vanished from the listing was closed to the authorizing
        # account. Its documents leave the index with it; the snapshot is the only
        # place their ids still exist on this side.
        for cabinet_id in sorted(set(previous_acls) - set(current_acls)):
            for container_id, ids in list(container_documents.items()):
                if not container_id.startswith(cabinet_id) and container_id != cabinet_id:
                    continue
                for document_id in ids:
                    yield NetDocumentsDocumentDeletionEntity(
                        document_id=document_id, deletion_status="removed", breadcrumbs=[]
                    )
                    changes += 1
                container_documents.pop(container_id, None)

        if cursor:
            current = NetDocumentsCursor(**cursor.data)
            current.modified_since = watermark
            current.last_entity_changes_count = changes
            current.cabinet_acls = current_acls
            current.container_documents = container_documents
            cursor.update(**current.model_dump())

        self.logger.info(f"Incremental sync complete: {changes} changes processed")

    async def _modified_documents(
        self,
        cabinet_id: str,
        modified_since: str,
        container_access: Optional[AccessControl],
        files: FileService | None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Documents in one cabinet modified since the watermark.

        The query uses the same syntax as the NetDocuments web interface, which is what
        the search endpoint documents. It finds edits and additions only: a deleted
        document stops matching every query rather than appearing as a tombstone, so
        deletions are handled by the caller's snapshot diff.
        """
        try:
            data = await self._get(
                f"{self._api_base_url}/v1/Search/{cabinet_id}",
                params={
                    "q": f"modified>={modified_since[:10]}",
                    "$select": CONTAINER_SELECT,
                    "top": PAGE_LIMIT,
                },
            )
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Modified-document search failed for {cabinet_id}: {e}")
            return

        breadcrumbs = [
            Breadcrumb(
                entity_id=cabinet_id, name=cabinet_id, entity_type="NetDocumentsCabinetEntity"
            )
        ]
        for record in self._entries(data):
            entity = await self._document_entity(
                record,
                cabinet_id=cabinet_id,
                container_id=cabinet_id,
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
        """List cabinets so an operator can pick which ones to sync.

        The cabinet is the unit of selection: it is the security boundary a firm
        actually reasons about, and picking below it would let a sync cross a wall the
        cabinet exists to draw.
        """
        if parent_node_id:
            return []
        nodes: List[BrowseNode] = []
        for record in await self._fetch_cabinets():
            identifier = str(record.get("id") or record.get("envId") or "").strip()
            if not identifier:
                continue
            nodes.append(
                BrowseNode(
                    source_node_id=identifier,
                    node_type="folder",
                    title=str(record.get("name") or identifier),
                    has_children=False,
                    node_metadata={"cabinet_id": identifier},
                )
            )
        return nodes

    def parse_browse_node_id(self, node_id: str) -> tuple:
        """NetDocuments browse ids are bare cabinet ids, so there is nothing to decode."""
        return "folder", {"cabinet_id": node_id}

    async def validate(self) -> None:
        """Validate credentials by asking NetDocuments who the token belongs to."""
        await self._get(f"{self._api_base_url}/v1/User/info")
