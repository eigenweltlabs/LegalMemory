"""Dropbox source implementation.

Dropbox is a file server in most firms that use it, so this connector is built around
the three things that follow from that: a whole estate is too large to re-walk, matter
folders are shared at the *folder* level, and both the files and the sharing change while
a sync is running.

**Traversal.** One ``files/list_folder`` call with ``recursive=true`` per synced root,
paginated through ``files/list_folder/continue``. An unscoped connection has one root
(the account root); a scoped one has a root per selected folder. Nothing is listed twice.

**Incremental sync.** The cursor returned by that listing is exactly what
``files/list_folder/continue`` later trades for the changes since. The cursor for the
*next* run is minted before the crawl starts (``files/list_folder/get_latest_cursor``),
so a file written while the crawl is in flight is replayed by the next delta drain
instead of being missed until the following full scan.

**Permissions.** A file's members are read from its parent shared folder
(``sharing/list_folder_members``) once and reused for every file inside it, because that
is how firms actually share and because one call per file would throttle the account.
A file shared on its own account (``has_explicit_shared_members``) gets its own
``sharing/list_file_members`` read. Dropbox groups named in those members are expanded
into their members through the team API so a group-shared matter is reachable by the
group, not by nobody.

**What the delta feed does not carry.** Adding somebody to a shared folder rewrites no
file's metadata, so no delta entry appears for it. Permission changes are therefore
picked up by the periodic full crawl the engine forces for exactly this reason
(``acl_refresh_hours``), not by the change feed.

Reference (Dropbox HTTP API v2):
  https://www.dropbox.com/developers/documentation/http/documentation
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.acl import dropbox_members_to_access
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.configs import DropboxConfig
from knowledge_index.connectors.cursors.dropbox import DropboxCursor
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.dropbox import (
    DropboxAccountEntity,
    DropboxFileDeletionEntity,
    DropboxFileEntity,
    DropboxFolderEntity,
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

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"

LIST_FOLDER = f"{API}/files/list_folder"
LIST_FOLDER_CONTINUE = f"{API}/files/list_folder/continue"
LIST_FOLDER_LATEST_CURSOR = f"{API}/files/list_folder/get_latest_cursor"
LIST_FILE_MEMBERS = f"{API}/sharing/list_file_members"
LIST_FILE_MEMBERS_CONTINUE = f"{API}/sharing/list_file_members/continue"
LIST_FOLDER_MEMBERS = f"{API}/sharing/list_folder_members"
LIST_FOLDER_MEMBERS_CONTINUE = f"{API}/sharing/list_folder_members/continue"
GROUPS_LIST = f"{API}/team/groups/list"
GROUPS_MEMBERS_LIST = f"{API}/team/groups/members/list"
GROUPS_MEMBERS_LIST_CONTINUE = f"{API}/team/groups/members/list/continue"
CURRENT_ACCOUNT = f"{API}/users/get_current_account"
DOWNLOAD = f"{CONTENT}/files/download"

# The account root. Dropbox names it with an empty string, not "/".
ACCOUNT_ROOT = ""

# Arguments every listing is opened with. They have to be identical between
# get_latest_cursor, list_folder and the cursor's later continue calls: a Dropbox cursor
# encodes the arguments of the listing that produced it, so drifting here would return a
# different estate than the one the cursor was minted against.
def _listing_args(path: str, *, recursive: bool = True) -> Dict[str, Any]:
    return {
        "path": path,
        "recursive": recursive,
        "include_deleted": False,
        "include_has_explicit_shared_members": True,
        "include_mounted_folders": True,
        "include_non_downloadable_files": True,
    }


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
    supports_continuous=True,
    cursor_class=DropboxCursor,
    supports_access_control=True,
    supports_browse_tree=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class DropboxSource(BaseSource):
    """Dropbox source connector: files, folder-level sharing, and a delta feed.

    Reads a Dropbox account read-only, mirrors who may open each file, and syncs
    incrementally from Dropbox's own listing cursors.
    """

    # A firm's Dropbox has few groups and many files, so re-expanding them on every
    # incremental run is affordable — and it is what bounds how long somebody removed
    # from a group keeps reaching that group's matters.
    cheap_memberships = True

    _exclude_path: str
    _mirror_permissions: bool
    _expand_team_groups: bool
    # Members are per shared folder, not per file: one read serves every file inside.
    _folder_access: Dict[str, Optional[AccessControl]]
    # Dropbox group principals ("dropbox:<group_id>") seen in this run's ACLs.
    _tracked_groups: set
    # Set once the team API refuses us, so a personal account does not pay a rejected
    # call per group for the rest of the run.
    _team_api_available: bool
    _owner_email: str

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
        instance._exclude_path = (config.exclude_path or "").strip().casefold()
        instance._mirror_permissions = config.mirror_permissions
        instance._expand_team_groups = config.expand_team_groups
        instance._folder_access = {}
        instance._tracked_groups = set()
        instance._team_api_available = True
        instance._owner_email = ""
        return instance

    # ------------------------------------------------------------------------ requests

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

    async def _paginate(
        self, url: str, args: Dict[str, Any], continue_url: str
    ) -> AsyncGenerator[Dict, None]:
        """Yield every entry of a paginated Dropbox listing."""
        data = await self._post(url, args)
        for entry in data.get("entries", []):
            yield entry
        while data.get("has_more"):
            data = await self._post(continue_url, {"cursor": data.get("cursor")})
            for entry in data.get("entries", []):
                yield entry

    # ------------------------------------------------------------------------- account

    async def _account(self) -> DropboxAccountEntity:
        entity = DropboxAccountEntity.from_api(await self._post(CURRENT_ACCOUNT, None))
        self._owner_email = str(entity.email or "").strip().casefold()
        return entity

    # -------------------------------------------------------------------------- access

    def _track_groups(self, access: Optional[AccessControl]) -> None:
        """Remember every Dropbox group a mirrored ACL grants read to.

        The grant on its own is unusable: nobody signs in to this appliance as a Dropbox
        group id. The tracked set is expanded into user memberships after the scan and
        persisted in the cursor so a delta run still knows what to expand.
        """
        for viewer in (access.viewers if access else None) or []:
            if viewer.startswith("group:dropbox:"):
                self._tracked_groups.add(viewer[len("group:") :])

    async def _members(self, url: str, args: Dict[str, Any], continue_url: str) -> Dict[str, list]:
        """Collect the users and groups of a sharing endpoint across its pages."""
        users: list = []
        groups: list = []
        data = await self._post(url, args)
        while True:
            users.extend(data.get("users") or [])
            groups.extend(data.get("groups") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
            data = await self._post(continue_url, {"cursor": cursor})
        return {"users": users, "groups": groups}

    async def _shared_folder_access(self, shared_folder_id: str) -> Optional[AccessControl]:
        """The members of one shared folder, read once per folder and cached.

        This is the call that makes permission mirroring affordable on a file server: a
        matter folder with four hundred documents costs one request, not four hundred.
        """
        if shared_folder_id in self._folder_access:
            return self._folder_access[shared_folder_id]
        try:
            members = await self._members(
                LIST_FOLDER_MEMBERS,
                {"shared_folder_id": shared_folder_id},
                LIST_FOLDER_MEMBERS_CONTINUE,
            )
            access = dropbox_members_to_access(
                members["users"], members["groups"], owner_email=self._owner_email
            )
        except SourceAuthError:
            raise
        except Exception as e:
            # Unknown, not empty. A throttled or refused read must leave the files
            # fail-closed rather than assert that nobody may open them.
            self.logger.warning(
                f"Could not read members of shared folder {shared_folder_id}: {e}; "
                "the files inside stay fail-closed for this run"
            )
            access = None
        # Cached either way: a folder that refused once will refuse for every file in it,
        # and retrying per file turns one failure into hundreds.
        self._folder_access[shared_folder_id] = access
        self._track_groups(access)
        return access

    async def _file_access(self, entry: Dict) -> Optional[AccessControl]:
        """Who may read one file.

        Three shapes, in the order Dropbox itself resolves them:

        1. the file was shared individually — read its own members;
        2. the file sits in a shared folder — reuse that folder's members;
        3. neither — it is private to the account, whose owner can plainly open it.

        Case 3 returns the owner rather than ``None``: "unknown" is for a permission read
        that failed, and reporting an unshared personal file as unknown would make a
        firm's own documents unreachable to the account that authorized the connection.
        """
        if not self._mirror_permissions:
            return None

        file_id = str(entry.get("id") or "")
        if entry.get("has_explicit_shared_members") and file_id:
            try:
                members = await self._members(
                    LIST_FILE_MEMBERS,
                    {"file": file_id, "include_inherited": True},
                    LIST_FILE_MEMBERS_CONTINUE,
                )
                access = dropbox_members_to_access(
                    members["users"], members["groups"], owner_email=self._owner_email
                )
                self._track_groups(access)
                return access
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Could not read sharing members for file {file_id}: {e}; "
                    "it stays fail-closed for this run"
                )
                return None

        shared_folder_id = str((entry.get("sharing_info") or {}).get("parent_shared_folder_id") or "")
        if shared_folder_id:
            return await self._shared_folder_access(shared_folder_id)

        if self._owner_email:
            return AccessControl(viewers=[f"user:{self._owner_email}"], is_public=False)
        # No owner address means nothing can be asserted about who may read this.
        return None

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand the Dropbox groups seen in this run's ACLs into their members.

        Without these rows a grant to ``group:dropbox:<id>`` matches no caller and a
        group-shared matter folder is invisible rather than protected. The team API is
        a Dropbox Business feature; on a personal account the first refusal turns the
        rest of the expansion off and the grants simply stay unmatched.
        """
        if not self._mirror_permissions or not self._expand_team_groups:
            return
        for group_ref in sorted(self._tracked_groups):
            if not self._team_api_available:
                return
            group_id = group_ref.partition(":")[2]
            if not group_id:
                continue
            async for membership in self._expand_group(group_id):
                yield membership

    async def _expand_group(self, group_id: str) -> AsyncGenerator[MembershipTuple, None]:
        """Yield one membership per member of a Dropbox group."""
        args: Dict[str, Any] = {"group": {".tag": "group_id", "group_id": group_id}}
        url, continue_url = GROUPS_MEMBERS_LIST, GROUPS_MEMBERS_LIST_CONTINUE
        group_name: Optional[str] = None
        while True:
            try:
                data = await self._post(url, args)
            except Exception as e:
                # Includes the 401 a personal account returns for every team endpoint,
                # which is why this does not re-raise SourceAuthError: the account's own
                # file credentials are fine, it simply has no team directory.
                self.logger.warning(
                    f"Could not expand Dropbox group {group_id}: {e}; files shared with "
                    "it stay unreachable through the group until it can be read"
                )
                self._team_api_available = False
                return
            group_name = group_name or (data.get("group_info") or {}).get("group_name")
            for member in data.get("members") or []:
                email = str((member.get("profile") or {}).get("email") or "").strip().casefold()
                if not email:
                    continue
                yield MembershipTuple(
                    member_id=email,
                    member_type="user",
                    group_id=f"dropbox:{group_id}",
                    group_name=group_name,
                )
            if not data.get("has_more"):
                return
            url, args = continue_url, {"cursor": data.get("cursor")}

    # ------------------------------------------------------------------------ download

    async def _download(self, entity: DropboxFileEntity, files: FileService) -> bool:
        """Stage one file's bytes. Returns True when the entity should be yielded.

        The download is pinned to the revision the listing reported, not to the path.
        A partner saving over a document while the crawl walks past it would otherwise
        stage the new bytes under the old revision's version token, and the index would
        hold content it believes it has already processed.
        """
        reference = f"rev:{entity.rev}" if entity.rev else entity.path_lower
        token = await self.auth.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            # Must be ASCII: json.dumps escapes non-ASCII by default, which is what
            # keeps a file named "Beschluss Müller.pdf" from producing an invalid header.
            "Dropbox-API-Arg": json.dumps({"path": reference}),
        }

        response = await self.http_client.post(DOWNLOAD, headers=headers)
        if response.status_code == 401 and self.auth.supports_refresh:
            headers["Authorization"] = f"Bearer {await self.auth.force_refresh()}"
            response = await self.http_client.post(DOWNLOAD, headers=headers)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
            context=f"downloading {entity.name}",
        )

        await files.save_bytes(
            entity=entity,
            content=response.content,
            filename_with_extension=entity.name,
            logger=self.logger,
        )
        if not entity.local_path:
            self.logger.warning(f"Save failed — no local path set for {entity.name}")
            return False
        return True

    # ----------------------------------------------------------------------- filtering

    def _excluded(self, path_lower: Optional[str]) -> bool:
        """Whether a path falls under the configured exclusion."""
        if not self._exclude_path:
            return False
        return self._exclude_path in (path_lower or "").casefold()

    async def _emit_file(
        self, entry: Dict, breadcrumbs: List[Breadcrumb], files: FileService
    ) -> AsyncGenerator[BaseEntity, None]:
        """Turn one ``list_folder`` file entry into a staged, permission-carrying entity."""
        if not entry.get("is_downloadable", True):
            # Paper docs and other server-side formats have no bytes to fetch; they need
            # the export API, which is a different content contract.
            self.logger.debug(
                f"Skipping non-downloadable file: {entry.get('path_display', 'unknown path')}"
            )
            return
        if self._excluded(entry.get("path_lower")):
            return

        entity = DropboxFileEntity.from_api(entry, breadcrumbs=breadcrumbs, download_url=DOWNLOAD)
        entity.access = await self._file_access(entry)

        try:
            if await self._download(entity, files):
                yield entity
        except FileSkippedException as e:
            self.logger.debug(f"Skipping file {entity.name}: {e.reason}")
        except SourceAuthError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            self.logger.warning(f"Skipping file {entity.name}: HTTP {e.response.status_code}")
        except Exception as e:
            # One unreadable document must never abort a firm's whole sync.
            self.logger.warning(f"Skipping file {entity.name}: {e}")

    # ---------------------------------------------------------------------------- scan

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

    async def _latest_cursor(self, root: str) -> str:
        """Mint a cursor for the root's *current* state without enumerating it.

        Called before the crawl. Everything written while the crawl runs is then replayed
        by the first delta drain; minting it afterwards would silently skip a document
        created mid-crawl in a folder the walk had already passed, until some later edit
        touched it again.
        """
        data = await self._post(LIST_FOLDER_LATEST_CURSOR, _listing_args(root))
        return str(data.get("cursor") or "")

    async def _crawl_root(
        self,
        root: str,
        account_breadcrumb: Breadcrumb,
        files: FileService,
        schema: DropboxCursor,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Yield everything under one root in a single recursive listing."""
        try:
            # Recorded the moment it is known rather than after the walk: an interrupted
            # crawl otherwise throws away a checkpoint it already paid a request for.
            # This is safe to keep because ``full_sync_required`` is only cleared once
            # the crawl finishes, so a half-finished run still crawls again next time.
            schema.update_root_cursor(root, await self._latest_cursor(root))
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Could not mint a change cursor for {root or '/'} ({e}); "
                "the next sync will crawl this root again"
            )

        breadcrumbs = [account_breadcrumb]
        try:
            async for entry in self._paginate(
                LIST_FOLDER, _listing_args(root), LIST_FOLDER_CONTINUE
            ):
                tag = entry.get(".tag")
                if tag == "folder":
                    if self._excluded(entry.get("path_lower")):
                        continue
                    yield DropboxFolderEntity.from_api(entry, breadcrumbs=breadcrumbs)
                elif tag == "file":
                    async for entity in self._emit_file(entry, breadcrumbs, files):
                        schema.remember_path(
                            str(getattr(entity, "path_lower", "") or ""),
                            str(getattr(entity, "id", "") or ""),
                        )
                        yield entity
        except SourceAuthError:
            raise
        except Exception as e:
            # Never widen back to the whole account: a selected folder that vanished is
            # one root fewer, not a licence to index a partner's personal files.
            self.logger.warning(
                f"Root {root or '/'} could not be listed ({e}); skipping it — the "
                "remaining roots still sync"
            )
            # The cursor minted for this root is discarded with it. Keeping it would let
            # the next run resume a delta from before a crawl that never happened, so
            # the root's existing files would not be re-observed by either pass.
            schema.root_cursors.pop(root, None)
            return

    async def _full_sync(
        self,
        schema: DropboxCursor,
        roots: List[str],
        files: FileService,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Crawl each root and seed the cursor the next delta run resumes from."""
        account = await self._account()
        yield account
        account_breadcrumb = Breadcrumb(
            entity_id=account.account_id,
            name=account.display_name,
            entity_type="DropboxAccountEntity",
        )

        # A crawl re-establishes the whole picture, so last run's cursors and paths are
        # superseded rather than merged. Merging would leave a path map describing files
        # that are no longer in scope.
        schema.root_cursors = {}
        schema.path_ids = {}

        count = 0
        for root in roots:
            async for entity in self._crawl_root(root, account_breadcrumb, files, schema):
                count += 1
                yield entity

        schema.mark_full_sync_done(entities=count)
        self.logger.info(f"Full sync complete: {count} entities across {len(roots)} root(s)")

    # ----------------------------------------------------------------------- delta feed

    async def _incremental_sync(
        self,
        schema: DropboxCursor,
        roots: List[str],
        files: FileService,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Drain each root's change feed and yield only what changed.

        Deletions are held back until the whole batch has been read. Dropbox reports a
        rename as a removal at the old path plus the file at its new one, and the file id
        is the same on both sides — emitting the removal would tombstone the document the
        same batch just updated, because the engine applies deletions after observations.
        """
        await self._account()

        changes = 0
        for root in roots:
            cursor = schema.root_cursors.get(root)
            if not cursor:
                self.logger.warning(f"No change cursor for root {root or '/'}; crawling instead")
                schema.mark_full_sync_required("missing root cursor")
                return

            observed_ids: set = set()
            deleted_ids: List[str] = []
            breadcrumbs = [
                Breadcrumb(
                    entity_id="dropbox",
                    name="Dropbox",
                    entity_type="DropboxAccountEntity",
                )
            ]

            while True:
                try:
                    data = await self._post(LIST_FOLDER_CONTINUE, {"cursor": cursor})
                except SourceAuthError:
                    raise
                except Exception as e:
                    # A cursor Dropbox has rejected ("reset") cannot be repaired here.
                    # Flagging the fall back durably is what keeps change tracking alive;
                    # pretending this run succeeded would stop it for good.
                    self.logger.warning(
                        f"Change feed for {root or '/'} could not be drained ({e}); "
                        "the next sync will crawl"
                    )
                    schema.mark_full_sync_required("rejected cursor")
                    return

                for entry in data.get("entries", []):
                    tag = entry.get(".tag")
                    if tag == "deleted":
                        path = str(entry.get("path_lower") or "")
                        if self._excluded(path):
                            continue
                        # One deleted folder stands for everything that was under it.
                        deleted_ids.extend(schema.ids_under(path))
                        continue
                    if tag != "file":
                        continue
                    async for entity in self._emit_file(entry, breadcrumbs, files):
                        file_id = str(getattr(entity, "id", "") or "")
                        path = str(getattr(entity, "path_lower", "") or "")
                        observed_ids.add(file_id)
                        schema.remember_path(path, file_id)
                        changes += 1
                        yield entity

                cursor = str(data.get("cursor") or cursor)
                if not data.get("has_more"):
                    break

            # Anything the same batch also observed was renamed or moved, not removed.
            removed = {file_id for file_id in deleted_ids if file_id not in observed_ids}
            for file_id in dict.fromkeys(deleted_ids):
                if file_id not in removed:
                    continue
                changes += 1
                yield DropboxFileDeletionEntity(
                    breadcrumbs=[],
                    file_id=file_id,
                    name="deleted",
                    deletion_status="removed",
                )
            # One pass over the map rather than one per deletion: a folder removal can
            # carry thousands of ids, and the map holds up to fifty thousand paths.
            for path in [path for path, tracked in schema.path_ids.items() if tracked in removed]:
                schema.forget_path(path)

            schema.update_root_cursor(root, cursor)

        if schema.path_map_overflowed():
            # Past this size the map costs more than the crawl it saves. Ask for one and
            # start the map again from what that crawl observes.
            self.logger.info(
                "Tracked Dropbox path map outgrew its bound; requesting a full crawl "
                "so deletions keep being resolvable"
            )
            schema.mark_full_sync_required("path map overflow")

        schema.mark_incremental_done(changes=changes)
        self.logger.info(f"Incremental sync complete: {changes} changes")

    # --------------------------------------------------------------------------- entry

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate Dropbox entities by crawling or by draining the change feed."""
        assert files is not None, "FileService is required for Dropbox"

        schema = DropboxCursor(**(cursor.data if cursor else {}))
        self._tracked_groups.update(str(group) for group in schema.tracked_groups)

        selected = self._selected_paths(node_selections)
        roots = selected or [ACCOUNT_ROOT]
        # A re-scope changes which roots the cursors describe, so resuming from them
        # would drain the change feed of folders nobody selected any more and never
        # enumerate the ones they did.
        if sorted(schema.root_cursors) != sorted(roots):
            schema.mark_full_sync_required("selected roots changed")

        scope = f"TARGETED ({len(selected)} folder roots) " if selected else ""
        if schema.needs_full_sync() or schema.needs_periodic_full_sync():
            self.logger.info(f"Sync strategy: {scope}FULL")
            generator = self._full_sync(schema, roots, files)
        else:
            self.logger.info(f"Sync strategy: {scope}INCREMENTAL")
            generator = self._incremental_sync(schema, roots, files)

        try:
            async for entity in generator:
                yield entity
        finally:
            # Written even when the caller stops early, so a partial run does not throw
            # away the cursor and the path map it did establish.
            if cursor is not None:
                schema.tracked_groups = sorted(self._tracked_groups)
                cursor.update(**schema.model_dump())

    # -------------------------------------------------------------------------- browse

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

        nodes: List[BrowseNode] = []
        async for entry in self._paginate(
            LIST_FOLDER, _listing_args(path, recursive=False), LIST_FOLDER_CONTINUE
        ):
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

    def parse_browse_node_id(self, node_id: str) -> tuple:
        """Dropbox browse ids are the folder paths themselves."""
        return "folder", {"path": node_id}

    async def validate(self) -> None:
        """Verify the token by calling /users/get_current_account (POST, no body)."""
        await self._post(CURRENT_ACCOUNT, None)
