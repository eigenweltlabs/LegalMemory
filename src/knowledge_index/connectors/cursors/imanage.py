"""iManage Work cursor schema.

iManage has no delta token either. What it has is a workspace and document search that
filters on ``edit_date``, so an incremental run asks each synced library for documents
edited since the last watermark.

Security is the other half. A firm walls a matter by changing the *workspace's*
security, and no document's ``edit_date`` moves when that happens. The mirrored access
per workspace is kept here and diffed on every incremental run, so a re-secured
workspace re-emits its documents at the policy interval rather than waiting for the
periodic full scan.
"""

from datetime import UTC, datetime

from pydantic import Field

from ._base import BaseCursor


class IManageCursor(BaseCursor):
    """iManage Work incremental sync cursor."""

    edited_since: str = Field(
        default="",
        description="ISO 8601 watermark: fetch documents edited after this instant.",
    )

    full_sync_required: bool = Field(
        default=True,
        description="Whether a full sync is required.",
    )

    last_full_sync_timestamp: str = Field(
        default="",
        description="ISO 8601 timestamp of last full sync.",
    )

    last_entity_changes_count: int = Field(
        default=0,
        description="Number of changes processed in the last incremental sync.",
    )

    tracked_groups: dict = Field(
        default_factory=dict,
        description=(
            "Groups that held read access on the last run, id -> library. Recorded for "
            "operators reading a sync's state; the next run rebuilds the set from the "
            "workspaces it reads rather than restoring this one, so a group whose "
            "access the firm has revoked stops being expanded."
        ),
    )

    workspace_acls: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of workspace_id -> sorted viewer principals. Diffed on every "
            "incremental run: a workspace whose security changed re-emits its "
            "documents, because re-securing a matter moves no document timestamp."
        ),
    )

    workspace_documents: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of workspace_id -> [document ids]. A document removed from a "
            "workspace has no tombstone in the search API, so this snapshot is the "
            "only place its id still exists on this side."
        ),
    )

    def needs_full_sync(self) -> bool:
        if self.full_sync_required:
            return True
        return not self.edited_since

    def needs_periodic_full_sync(self, interval_days: int = 7) -> bool:
        if not self.last_full_sync_timestamp:
            return True
        try:
            last_full = datetime.fromisoformat(self.last_full_sync_timestamp)
            if last_full.tzinfo is None:
                last_full = last_full.replace(tzinfo=UTC)
            return (datetime.now(UTC) - last_full).days >= interval_days
        except (ValueError, TypeError):
            return True
