"""OneDrive cursor schema for incremental sync.

OneDrive shares SharePoint's change model: Microsoft Graph delta queries per drive
(/drives/{id}/root/delta). A personal or business OneDrive usually has exactly one
drive, but /me/drives can legitimately return several, so the token map is keyed by
drive id rather than assuming one.
"""

from datetime import UTC, datetime
from typing import Dict, List

from pydantic import Field

from ._base import BaseCursor


class OneDriveCursor(BaseCursor):
    """OneDrive incremental sync cursor.

    Tracks two independent change streams:
    1. Entity sync via Graph delta queries (per-drive delta tokens)
    2. ACL sync via tracked Entra groups whose memberships are re-mirrored
    """

    drive_delta_tokens: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of drive_id -> delta token from Graph delta query.",
    )

    last_entity_sync_timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of last successful entity sync.",
    )

    last_entity_changes_count: int = Field(
        default=0,
        description="Number of entity changes processed in last incremental sync.",
    )

    full_sync_required: bool = Field(
        default=True,
        description="Whether a full sync is required.",
    )

    last_full_sync_timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of last full sync.",
    )

    total_entities_synced: int = Field(
        default=0,
        description="Total entities synced in last full sync.",
    )

    synced_drive_ids: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of drive_id -> drive name (discovered drives).",
    )

    tracked_entra_groups: List[str] = Field(
        default_factory=list,
        description=(
            "Entra group ids seen in mirrored item ACLs ('entra:<guid>'). Kept in the "
            "cursor so an incremental run still knows which groups to expand into "
            "memberships; forgetting them would silently drop group-shared access."
        ),
    )

    def needs_full_sync(self) -> bool:
        """Return whether a full sync is needed (flag set or no delta tokens)."""
        if self.full_sync_required:
            return True
        if not self.drive_delta_tokens:
            return True
        return False

    def needs_periodic_full_sync(self, interval_days: int = 7) -> bool:
        """Return whether enough time has elapsed to warrant a periodic full sync."""
        if not self.last_full_sync_timestamp:
            return True
        try:
            last_full = datetime.fromisoformat(self.last_full_sync_timestamp)
            if last_full.tzinfo is None:
                last_full = last_full.replace(tzinfo=UTC)
            elapsed = datetime.now(UTC) - last_full
            return elapsed.days >= interval_days
        except (ValueError, TypeError):
            return True

    def update_entity_cursor(
        self,
        drive_id: str,
        delta_token: str,
        changes_count: int,
        is_full_sync: bool = False,
    ) -> None:
        """Update entity sync state for a given drive."""
        self.drive_delta_tokens[drive_id] = delta_token
        self.last_entity_sync_timestamp = datetime.now(UTC).isoformat()
        self.last_entity_changes_count = changes_count
        if is_full_sync:
            self.last_full_sync_timestamp = datetime.now(UTC).isoformat()
            self.total_entities_synced = changes_count
            self.full_sync_required = False

    def mark_full_sync_required(self, reason: str = "") -> None:
        """Flag that a full sync is needed on the next run."""
        self.full_sync_required = True
