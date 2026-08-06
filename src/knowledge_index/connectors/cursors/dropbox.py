"""Dropbox cursor schema for incremental sync.

Dropbox's change model is a per-listing cursor: ``files/list_folder`` returns one, and
``files/list_folder/continue`` trades it for everything that changed since. A cursor is
bound to the arguments the listing was opened with, so a connection scoped to three
matter folders holds three cursors, one per root, and an unscoped connection holds one
for the account root.

Two things are kept alongside the cursors, and both exist because of what Dropbox's
delta feed leaves out:

**``path_ids``** — Dropbox reports a removal as ``{".tag": "deleted", "path_lower": ...}``
with **no file id**. The index is keyed by file id (ids survive rename and move; paths do
not), so a deletion is only actionable if the path can be mapped back to the id that was
indexed under it. That map is built as files are observed and consulted when they are
removed.

**``tracked_groups``** — a file shared with a Dropbox group is granted to
``group:dropbox:<group_id>``, which matches no caller until the group's members are
mirrored. The set is persisted so an incremental run still knows which groups to expand;
forgetting it would silently drop group-shared access on the first delta sync.
"""

from datetime import UTC, datetime
from typing import Dict, List

from pydantic import Field

from ._base import BaseCursor

# Above this many tracked paths the map is dropped and a full scan is requested instead.
# The map exists only to make deletions actionable *between* full scans; the engine's
# own full-scan diff tombstones whatever the delta feed could not resolve. Trading an
# unbounded cursor for one extra crawl is the right side of that bargain: cursors are
# persisted as one row per source, and a firm with a million files would otherwise write
# tens of megabytes of JSON on every sync.
MAX_TRACKED_PATHS = 50_000


class DropboxCursor(BaseCursor):
    """Dropbox incremental sync cursor."""

    root_cursors: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of synced root path -> Dropbox list_folder cursor. The account root is "
            "keyed as the empty string, exactly as Dropbox names it."
        ),
    )

    path_ids: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of path_lower -> file id for indexed files, so a delta deletion (which "
            "carries a path but no id) can be resolved to the object it removed."
        ),
    )

    tracked_groups: List[str] = Field(
        default_factory=list,
        description=(
            "Dropbox group principals ('dropbox:<group_id>') seen in mirrored ACLs, kept "
            "so an incremental run still expands them into memberships."
        ),
    )

    acting_identity: str = Field(
        default="",
        description=(
            "The identity the estate was read as — token kind, acting team member and "
            "namespace. A change means the stored cursors and path map describe a "
            "different estate, so the next run crawls instead of resuming."
        ),
    )

    full_sync_required: bool = Field(
        default=True,
        description="Whether the next run must crawl instead of draining the delta feed.",
    )

    last_full_sync_timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of the last completed full crawl.",
    )

    last_entity_sync_timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of the last completed sync of any mode.",
    )

    last_entity_changes_count: int = Field(
        default=0,
        description="Number of changes processed in the last incremental sync.",
    )

    total_entities_synced: int = Field(
        default=0,
        description="Entities yielded by the last full crawl.",
    )

    def needs_full_sync(self) -> bool:
        """Whether a crawl is required rather than a delta drain."""
        return self.full_sync_required or not self.root_cursors

    def needs_periodic_full_sync(self, interval_days: int = 7) -> bool:
        """Whether enough time has passed to re-crawl regardless of the delta feed.

        A Dropbox cursor never reports a sharing change made on a *parent* folder: adding
        someone to a shared folder rewrites no file's metadata, so no entry appears. The
        periodic crawl is what bounds how long a stale grant can survive; the engine's own
        ``acl_refresh_hours`` does the same job on a shorter clock.
        """
        if not self.last_full_sync_timestamp:
            return True
        try:
            last_full = datetime.fromisoformat(self.last_full_sync_timestamp)
        except (ValueError, TypeError):
            return True
        if last_full.tzinfo is None:
            last_full = last_full.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last_full).days >= interval_days

    # -- path map ----------------------------------------------------------------

    def remember_path(self, path_lower: str, file_id: str) -> None:
        """Record which id is indexed at a path, so its deletion can be resolved."""
        if not path_lower or not file_id:
            return
        self.path_ids[path_lower.casefold()] = file_id

    def forget_path(self, path_lower: str) -> None:
        self.path_ids.pop((path_lower or "").casefold(), None)

    def ids_under(self, path_lower: str) -> List[str]:
        """Every indexed id at ``path_lower`` or beneath it.

        Deleting a folder produces one ``deleted`` entry for the folder itself and none
        for its contents, so the descendants have to be resolved here or a removed matter
        folder would stay searchable in full.
        """
        path = (path_lower or "").casefold().rstrip("/")
        if not path:
            return []
        prefix = f"{path}/"
        return [
            file_id
            for tracked, file_id in self.path_ids.items()
            if tracked == path or tracked.startswith(prefix)
        ]

    def path_map_overflowed(self) -> bool:
        return len(self.path_ids) > MAX_TRACKED_PATHS

    # -- bookkeeping -------------------------------------------------------------

    def update_root_cursor(self, root: str, cursor: str) -> None:
        if cursor:
            self.root_cursors[root] = cursor

    def mark_full_sync_done(self, *, entities: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.last_full_sync_timestamp = now
        self.last_entity_sync_timestamp = now
        self.total_entities_synced = entities
        self.full_sync_required = False

    def mark_incremental_done(self, *, changes: int) -> None:
        self.last_entity_sync_timestamp = datetime.now(UTC).isoformat()
        self.last_entity_changes_count = changes

    def mark_full_sync_required(self, reason: str = "") -> None:
        """Flag that the delta feed cannot be trusted and the next run must crawl."""
        self.full_sync_required = True
        # The cursors are dropped with the flag: keeping a cursor Dropbox has already
        # rejected would let a later run resume from it and silently skip everything
        # that changed while it was invalid.
        self.root_cursors = {}
