"""Clio cursor schema for incremental sync.

Clio's change feed is a filter, not a token: ``documents.json?updated_since=T`` with
``include_deleted=true`` returns everything created, changed, or deleted after T. The
watermark for the next run is minted *before* the crawl, so a document that changes
while the crawl is running is replayed by the first incremental drain rather than
lost until the next full scan.
"""

from datetime import UTC, datetime

from pydantic import Field

from ._base import BaseCursor


class ClioCursor(BaseCursor):
    """Clio incremental sync cursor."""

    updated_since: str = Field(
        default="",
        description="ISO 8601 watermark: fetch documents changed after this instant.",
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
            "Clio permission groups seen in mirrored ACLs, id -> name. Kept so an "
            "incremental run still knows which groups keep restricted matters "
            "reachable."
        ),
    )

    matter_groups: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of matter_id -> permission group id ('' when unrestricted). "
            "Diffed on every incremental run so a re-permissioned matter re-emits its "
            "documents within the policy interval instead of waiting for the daily "
            "full refresh."
        ),
    )

    matter_documents: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of matter_id -> [document ids]. A matter that vanishes from the "
            "listing (walled away from the authorizing user) has no other way to name "
            "the documents that must leave the index with it."
        ),
    )

    def needs_full_sync(self) -> bool:
        if self.full_sync_required:
            return True
        return not self.updated_since

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
