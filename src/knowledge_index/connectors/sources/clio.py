"""Clio source implementation using the Clio Manage API v4.

Retrieves a firm's Clio estate:
 - Matters (ClioMatterEntity containers, giving documents their breadcrumb)
 - Documents (ClioDocumentEntity files, downloaded via the 303 presigned redirect)

Incremental sync:
 - ``documents.json?updated_since=T&include_deleted=true`` is a real change feed,
   deletions included. The watermark for the next run is minted before the crawl so a
   document changed mid-crawl replays on the first incremental drain.

Access graph generation:
 - Clio's API exposes no per-matter viewer lists; its permission mechanism is the
   *group*: a matter (or an individual document) carrying a permission group is
   restricted to that group's members, and everything else is firm-wide. The mirror
   follows exactly that: ``group:clio:{id}`` viewers for grouped content, firm-wide
   (``role:authenticated``) otherwise, with group memberships expanded from
   ``groups.json``. Firm administrators who can bypass restrictions inside Clio are
   deliberately not mirrored — narrower than the source is fail-closed, wider is not.
 - The connection boundary does the rest: the API only returns matters the
   authorizing user can see, so a restricted matter outside that user's visibility is
   never fetched at all.

Reference: https://docs.developers.clio.com/api-reference/
"""

from datetime import UTC, datetime
from typing import AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.configs import ClioConfig
from knowledge_index.connectors.cursors.clio import ClioCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.clio import (
    ClioDocumentDeletionEntity,
    ClioDocumentEntity,
    ClioMatterEntity,
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

# The association fields one document listing carries. Matter and group are what the
# access mirror needs; the rest is entity metadata. Requested explicitly because Clio
# returns only ``id`` and ``etag`` without a fields parameter.
DOCUMENT_FIELDS = (
    "id,etag,name,filename,content_type,size,created_at,updated_at,deleted_at,"
    "matter{id,display_number,description},group{id,name},"
    "document_category{id,name},parent{id,type}"
)
MATTER_FIELDS = (
    "id,etag,display_number,description,status,created_at,updated_at,"
    "practice_area{id,name},client{id,name},group{id,name}"
)
PAGE_LIMIT = 200


@source(
    name="Clio",
    short_name="clio",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    auth_config_class=None,
    config_class=ClioConfig,
    labels=["Practice Management", "Legal"],
    supports_continuous=True,
    cursor_class=ClioCursor,
    supports_access_control=True,
    supports_browse_tree=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class ClioSource(BaseSource):
    """Clio source connector: matters and their documents from Clio Manage."""

    # One matters listing plus one call per referenced group: cheap enough to refresh
    # memberships on every sync, so a wall change lands at the policy interval rather
    # than the daily full refresh.
    cheap_memberships = True

    _api_base_url: str
    _mirror_permissions: bool
    # Permission groups seen in mirrored ACLs this run, id -> name. Expanded into
    # memberships afterwards and persisted in the cursor.
    _tracked_groups: Dict[str, str]

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: ClioConfig,
    ) -> "ClioSource":
        """Create a new Clio source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._api_base_url = config.api_base_url.rstrip("/")
        instance._mirror_permissions = config.mirror_permissions
        instance._tracked_groups = {}
        return instance

    @property
    def _auth_hosts(self) -> Tuple[str, ...]:
        """The only host the firm's bearer token may be sent to.

        Downloads answer with a 303 to a presigned storage URL; that URL carries its
        own credential and must not also receive the firm's token.
        """
        return (urlparse(self._api_base_url).netloc,)

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[Dict] = None) -> Dict:
        """Make an authenticated GET request to the Clio API with retry logic."""
        token = await self.auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            self.logger.warning(f"Got 401 from Clio at {url}, refreshing token...")
            new_token = await self.auth.force_refresh()
            headers = {"Authorization": f"Bearer {new_token}"}
            response = await self.http_client.get(url, headers=headers, params=params)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    async def _paginate(
        self, path: str, params: Optional[Dict] = None
    ) -> AsyncGenerator[Dict, None]:
        """Yield records from a Clio list endpoint, following ``meta.paging.next``."""
        url: Optional[str] = f"{self._api_base_url}{path}"
        query = dict(params or {})
        query.setdefault("limit", PAGE_LIMIT)
        while url:
            data = await self._get(url, params=query)
            for record in data.get("data", []):
                yield record
            url = ((data.get("meta") or {}).get("paging") or {}).get("next")
            # The next link carries the whole query.
            query = None

    # ------------------------------------------------------------------ access mirror

    def _record_access(self, record: Dict, matter_group: Optional[Dict]) -> Optional[AccessControl]:
        """The mirrored ACL for one document record.

        The document's own permission group wins; otherwise the matter's; otherwise
        the content is unrestricted, which in Clio means the whole firm — every
        authenticated user of this single-firm appliance.
        """
        if not self._mirror_permissions:
            return None
        group = record.get("group") or matter_group or {}
        group_id = group.get("id")
        if group_id is None:
            return AccessControl(viewers=[], is_public=True)
        self._tracked_groups[str(group_id)] = str(group.get("name") or "")
        return AccessControl(viewers=[f"group:clio:{group_id}"], is_public=False)

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand tracked Clio permission groups into user memberships.

        Without these rows a grant to ``group:clio:{id}`` matches no caller and a
        restricted matter's documents are invisible rather than protected.
        """
        if not self._mirror_permissions or not self._tracked_groups:
            return
        self.logger.info(f"Expanding {len(self._tracked_groups)} Clio permission groups")
        for group_id in sorted(self._tracked_groups):
            try:
                data = await self._get(
                    f"{self._api_base_url}/groups/{group_id}.json",
                    params={"fields": "id,name,users{id,email,name}"},
                )
            except SourceAuthError:
                raise
            except Exception as e:
                # Fail-closed: an unexpandable group grants nobody, and a later
                # healthy run restores the members.
                self.logger.warning(f"Could not expand Clio group {group_id}: {e}")
                continue
            group = data.get("data") or {}
            group_name = str(group.get("name") or self._tracked_groups[group_id] or group_id)
            for user in group.get("users") or []:
                email = str(user.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                yield MembershipTuple(
                    member_id=email,
                    member_type="user",
                    group_id=f"clio:{group_id}",
                    group_name=group_name,
                )

    # ------------------------------------------------------------------------- crawl

    @staticmethod
    def _selected_matter_ids(
        node_selections: Optional[List[NodeSelectionData]],
    ) -> List[str]:
        """The matter ids this connection was scoped to."""
        matter_ids: List[str] = []
        for selection in node_selections or []:
            metadata = selection.node_metadata or {}
            matter_id = str(metadata.get("matter_id") or selection.source_node_id or "").strip()
            if matter_id and matter_id not in matter_ids:
                matter_ids.append(matter_id)
        return matter_ids

    async def _fetch_matters(
        self, matter_ids: Optional[List[str]] = None
    ) -> AsyncGenerator[Dict, None]:
        if matter_ids:
            for matter_id in matter_ids:
                try:
                    data = await self._get(
                        f"{self._api_base_url}/matters/{matter_id}.json",
                        params={"fields": MATTER_FIELDS},
                    )
                    yield data.get("data") or {}
                except SourceAuthError:
                    raise
                except Exception as e:
                    # Never widen: a selected matter that vanished or was restricted
                    # away costs that one root, not the run — and not the whole firm.
                    self.logger.warning(
                        f"Selected matter {matter_id} could not be read ({e}); "
                        "skipping it — the remaining selected matters still sync"
                    )
        else:
            async for record in self._paginate(
                "/matters.json", {"fields": MATTER_FIELDS}
            ):
                yield record

    def _matter_entity(self, record: Dict) -> Optional[ClioMatterEntity]:
        identifier = record.get("id")
        if identifier is None:
            return None
        return ClioMatterEntity(
            breadcrumbs=[],
            matter_id=str(identifier),
            display_number=str(record.get("display_number") or identifier),
            description=record.get("description"),
            status=record.get("status"),
            practice_area=(record.get("practice_area") or {}).get("name"),
            client_name=(record.get("client") or {}).get("name"),
            created_at=record.get("created_at"),
            updated_at=record.get("updated_at"),
        )

    async def _document_entities(
        self,
        params: Dict,
        matter_groups: Dict[str, Optional[Dict]],
        matter_names: Dict[str, str],
        files: FileService | None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Stream documents for one listing query, ACLs mirrored, bytes staged."""
        async for record in self._paginate("/documents.json", params):
            matter = record.get("matter") or {}
            matter_id = str(matter["id"]) if matter.get("id") is not None else None
            breadcrumbs = []
            if matter_id:
                breadcrumbs = [
                    Breadcrumb(
                        entity_id=matter_id,
                        name=matter_names.get(matter_id)
                        or str(matter.get("display_number") or matter_id),
                        entity_type="ClioMatterEntity",
                    )
                ]
            entity = ClioDocumentEntity.from_api(
                record, api_base_url=self._api_base_url, breadcrumbs=breadcrumbs
            )
            if entity is None:
                continue
            entity.access = self._record_access(
                record, matter_groups.get(matter_id) if matter_id else None
            )
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
                        continue
                except FileSkippedException as e:
                    self.logger.debug(f"Skipping document {entity.name}: {e.reason}")
                    continue
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 401:
                        raise
                    self.logger.warning(
                        f"HTTP {e.response.status_code} downloading {entity.name}: {e}"
                    )
                    continue
            yield entity

    def _should_do_full_sync(self, cursor: SyncCursor | None) -> Tuple[bool, str]:
        cursor_data = cursor.data if cursor else {}
        if not cursor_data:
            return True, "no cursor data (first sync)"
        schema = ClioCursor(**cursor_data)
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
        """Generate all Clio entities using full, targeted, or incremental sync."""
        cursor_data = cursor.data if cursor else {}
        for group_id, name in (cursor_data.get("tracked_groups") or {}).items():
            self._tracked_groups[str(group_id)] = str(name or "")

        selected_matter_ids = self._selected_matter_ids(node_selections)
        is_full, reason = self._should_do_full_sync(cursor)
        scope_label = (
            f"TARGETED ({len(selected_matter_ids)} matters) " if selected_matter_ids else ""
        )
        self.logger.info(
            f"Sync strategy: {scope_label}{'FULL' if is_full else 'INCREMENTAL'} ({reason})"
        )

        if is_full:
            async for entity in self._full_sync(cursor, files, selected_matter_ids):
                yield entity
        else:
            async for entity in self._incremental_sync(cursor, files, selected_matter_ids):
                yield entity

        if cursor:
            cursor.update(tracked_groups=dict(self._tracked_groups))

    @staticmethod
    def _group_key(group: Optional[Dict]) -> str:
        """One matter's permission state as a comparable string ('' = unrestricted)."""
        group_id = (group or {}).get("id")
        return str(group_id) if group_id is not None else ""

    async def _full_sync(
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected_matter_ids: List[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        # Minted before the crawl: a document changed while the crawl runs is replayed
        # by the first incremental drain instead of lost until the periodic full scan.
        watermark = datetime.now(UTC).isoformat()

        matter_groups: Dict[str, Optional[Dict]] = {}
        matter_names: Dict[str, str] = {}
        matter_documents: Dict[str, List[str]] = {}
        entity_count = 0
        async for record in self._fetch_matters(selected_matter_ids or None):
            entity = self._matter_entity(record)
            if entity is None:
                continue
            matter_groups[entity.matter_id] = record.get("group")
            matter_names[entity.matter_id] = entity.display_number
            yield entity
            entity_count += 1

        def remember(entity: ClioDocumentEntity) -> None:
            if entity.matter_id:
                matter_documents.setdefault(entity.matter_id, []).append(entity.document_id)

        if selected_matter_ids:
            for matter_id in selected_matter_ids:
                if matter_id not in matter_groups:
                    continue
                async for entity in self._document_entities(
                    {"fields": DOCUMENT_FIELDS, "matter_id": matter_id},
                    matter_groups,
                    matter_names,
                    files,
                ):
                    remember(entity)
                    yield entity
                    entity_count += 1
        else:
            async for entity in self._document_entities(
                {"fields": DOCUMENT_FIELDS}, matter_groups, matter_names, files
            ):
                remember(entity)
                yield entity
                entity_count += 1

        if cursor:
            schema = ClioCursor(**cursor.data)
            schema.updated_since = watermark
            schema.full_sync_required = False
            schema.last_full_sync_timestamp = datetime.now(UTC).isoformat()
            schema.last_entity_changes_count = entity_count
            schema.matter_groups = {
                matter_id: self._group_key(group) for matter_id, group in matter_groups.items()
            }
            schema.matter_documents = matter_documents
            cursor.update(**schema.model_dump())

        self.logger.info(f"Full sync complete: {entity_count} entities")

    async def _incremental_sync(  # noqa: C901
        self,
        cursor: SyncCursor | None,
        files: FileService | None,
        selected_matter_ids: List[str],
    ) -> AsyncGenerator[BaseEntity, None]:
        """Drain the document change feed — and diff the permission structure.

        Clio's ``updated_since`` feed reports document edits, but a wall is built by
        changing a *matter*: its group flips, or it leaves the authorizing user's
        visibility entirely, and no document timestamp moves. The matter listing is
        one cheap call, so every incremental run re-reads it and diffs against the
        cursor's snapshot: re-permissioned and newly visible matters re-emit their
        documents, vanished matters delete theirs. Access changes therefore land at
        the policy interval; the daily full refresh remains the backstop.
        """
        cursor_data = cursor.data if cursor else {}
        schema = ClioCursor(**cursor_data)
        if not schema.updated_since:
            async for entity in self._full_sync(cursor, files, selected_matter_ids):
                yield entity
            return

        watermark = datetime.now(UTC).isoformat()
        selected = set(selected_matter_ids)
        changes = 0
        emitted: set[str] = set()

        previous_groups = {str(k): str(v) for k, v in (schema.matter_groups or {}).items()}
        matter_documents = {
            str(k): [str(item) for item in v]
            for k, v in (schema.matter_documents or {}).items()
        }

        # ---- permission pre-pass: one matters listing, diffed against the snapshot
        matter_groups: Dict[str, Optional[Dict]] = {}
        matter_names: Dict[str, str] = {}
        try:
            async for record in self._fetch_matters(selected_matter_ids or None):
                matter_id = str(record.get("id") or "")
                if not matter_id:
                    continue
                matter_groups[matter_id] = record.get("group")
                matter_names[matter_id] = str(record.get("display_number") or matter_id)
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Matter listing failed: {e}")
            if cursor:
                cursor.update(full_sync_required=True)
            return

        current_groups = {
            matter_id: self._group_key(group) for matter_id, group in matter_groups.items()
        }
        vanished = sorted(set(previous_groups) - set(current_groups))
        appeared = sorted(set(current_groups) - set(previous_groups))
        regrouped = sorted(
            matter_id
            for matter_id in set(current_groups) & set(previous_groups)
            if current_groups[matter_id] != previous_groups[matter_id]
        )
        if vanished or appeared or regrouped:
            self.logger.info(
                f"Matter permission diff: {len(vanished)} vanished, "
                f"{len(appeared)} appeared, {len(regrouped)} re-permissioned"
            )

        for matter_id in vanished:
            # Walled away from the authorizing user: its documents leave with it. The
            # snapshot is the only place their ids still exist on this side.
            for document_id in matter_documents.pop(matter_id, []):
                yield ClioDocumentDeletionEntity(
                    document_id=document_id, deletion_status="removed", breadcrumbs=[]
                )
                emitted.add(document_id)
                changes += 1

        for matter_id in appeared + regrouped:
            refreshed: List[str] = []
            async for entity in self._document_entities(
                {"fields": DOCUMENT_FIELDS, "matter_id": matter_id},
                matter_groups,
                matter_names,
                files,
            ):
                refreshed.append(entity.document_id)
                emitted.add(entity.document_id)
                yield entity
                changes += 1
            previous = set(matter_documents.get(matter_id, []))
            for document_id in sorted(previous - set(refreshed)):
                yield ClioDocumentDeletionEntity(
                    document_id=document_id, deletion_status="removed", breadcrumbs=[]
                )
                emitted.add(document_id)
                changes += 1
            matter_documents[matter_id] = refreshed

        # ---- the document change feed
        try:
            async for record in self._paginate(
                "/documents.json",
                {
                    "fields": DOCUMENT_FIELDS,
                    "updated_since": schema.updated_since,
                    "include_deleted": "true",
                },
            ):
                identifier = record.get("id")
                if identifier is None or str(identifier) in emitted:
                    continue
                document_id = str(identifier)
                matter = record.get("matter") or {}
                matter_id = str(matter["id"]) if matter.get("id") is not None else None

                if record.get("deleted_at"):
                    yield ClioDocumentDeletionEntity(
                        document_id=document_id, deletion_status="removed", breadcrumbs=[]
                    )
                    changes += 1
                    if matter_id and document_id in matter_documents.get(matter_id, []):
                        matter_documents[matter_id].remove(document_id)
                    continue

                if selected and (matter_id is None or matter_id not in selected):
                    # Moved or filed outside the selected matters: remove the indexed
                    # copy. Harmless no-op for documents that were never inside.
                    yield ClioDocumentDeletionEntity(
                        document_id=document_id, deletion_status="removed", breadcrumbs=[]
                    )
                    changes += 1
                    continue

                unknown_matter = matter_id is not None and matter_id not in matter_groups
                group = matter_groups.get(matter_id) if matter_id else None
                breadcrumbs = []
                if matter_id:
                    breadcrumbs = [
                        Breadcrumb(
                            entity_id=matter_id,
                            name=matter_names.get(matter_id)
                            or str(matter.get("display_number") or matter_id),
                            entity_type="ClioMatterEntity",
                        )
                    ]
                entity = ClioDocumentEntity.from_api(
                    record, api_base_url=self._api_base_url, breadcrumbs=breadcrumbs
                )
                if entity is None:
                    continue
                # A document whose matter the listing cannot see keeps unknown access
                # (fail-closed), never firm-wide by default.
                entity.access = None if unknown_matter else self._record_access(record, group)
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
                            continue
                    except FileSkippedException:
                        continue
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 401:
                            raise
                        self.logger.warning(f"Download failed for {entity.name}: {e}")
                        continue
                if matter_id:
                    known = matter_documents.setdefault(matter_id, [])
                    if document_id not in known:
                        known.append(document_id)
                yield entity
                changes += 1
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Incremental listing failed: {e}")
            if cursor:
                cursor.update(full_sync_required=True)
            return

        if cursor:
            current = ClioCursor(**cursor.data)
            current.updated_since = watermark
            current.last_entity_changes_count = changes
            current.matter_groups = current_groups
            current.matter_documents = {
                matter_id: ids
                for matter_id, ids in matter_documents.items()
                if matter_id in current_groups
            }
            cursor.update(**current.model_dump())

        self.logger.info(f"Incremental sync complete: {changes} changes processed")

    # ------------------------------------------------------------------------- browse

    async def get_browse_children(
        self,
        parent_node_id: Optional[str] = None,
    ) -> List[BrowseNode]:
        """List matters so an operator can pick which ones to sync.

        Matters are the unit of selection — a Clio estate is scoped by matter, not by
        folder — so the tree is one flat level.
        """
        if parent_node_id:
            return []
        nodes: List[BrowseNode] = []
        async for record in self._paginate(
            "/matters.json",
            {"fields": "id,display_number,description,status"},
        ):
            identifier = record.get("id")
            if identifier is None:
                continue
            title = str(record.get("display_number") or identifier)
            description = record.get("description")
            nodes.append(
                BrowseNode(
                    source_node_id=str(identifier),
                    node_type="folder",
                    title=f"{title} — {description}" if description else title,
                    has_children=False,
                    node_metadata={"matter_id": str(identifier)},
                )
            )
        return nodes

    def parse_browse_node_id(self, node_id: str) -> tuple:
        """Clio browse ids are bare matter ids, so there is nothing to decode."""
        return "folder", {"matter_id": node_id}

    async def validate(self) -> None:
        """Validate Clio credentials by asking who the token belongs to."""
        await self._get(f"{self._api_base_url}/users/who_am_i.json")
