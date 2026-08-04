"""NetDocuments cursor schema.

NetDocuments has no delta token. What it has is a cabinet search that accepts the same
query language as the web interface, so an incremental run asks each synced cabinet for
documents modified since the last watermark. That finds edits and additions; it cannot
report deletions, because a deleted document stops matching every query rather than
appearing as a tombstone.

Deletions are therefore reconciled the only way this API allows: the ids seen per
container are kept here, and a container re-listed on an incremental run yields
deletions for the ids that no longer appear. The periodic full scan remains the
backstop for a container that was itself removed.
"""

from datetime import UTC, datetime

from pydantic import Field

from ._base import BaseCursor


class NetDocumentsCursor(BaseCursor):
    """NetDocuments incremental sync cursor."""

    modified_since: str = Field(
        default="",
        description="ISO 8601 watermark: fetch documents modified after this instant.",
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
            "Groups that held read rights on the last run, id -> name. Recorded for "
            "operators reading a sync's state, and deliberately not fed back into the "
            "next run: every run re-reads every synced cabinet's membership, so "
            "restoring this set would keep expanding a group whose rights the firm "
            "has since revoked."
        ),
    )

    cabinet_acls: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of cabinet_id -> sorted viewer principals. Diffed on every "
            "incremental run: a cabinet whose membership changed re-emits its "
            "documents, so a wall built or removed in NetDocuments lands at the "
            "policy interval instead of waiting for the periodic full scan."
        ),
    )

    container_documents: dict = Field(
        default_factory=dict,
        description=(
            "Snapshot of container_id -> [document ids]. A document removed from a "
            "container has no tombstone in the search API, so this snapshot is the "
            "only place its id still exists on this side."
        ),
    )

    def needs_full_sync(self) -> bool:
        if self.full_sync_required:
            return True
        return not self.modified_since

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
