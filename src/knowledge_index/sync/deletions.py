"""Deletions large enough to need a second opinion, held until scans agree.

A single scan cannot distinguish "the firm deleted this matter" from "the connector
enumerated one site out of forty". The engine used to refuse the whole scan in that case,
which protected the index but left a genuine bulk deletion permanently un-applyable
without hand-editing the database.

So the large deletion is *confirmed* instead of refused: the set of external ids the scan
believes is gone is recorded, the objects stay indexed and searchable, and the tombstones
are applied only once N consecutive scans have reported the **identical set**.

The set, not the count. A connector that loses 340 objects today and a different 340
tomorrow is malfunctioning; counting totals would average that into a deletion and remove
680 documents that still exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from knowledge_index.db.models import SourceDeletionCandidate, SourceDeletionWatch


@dataclass(frozen=True)
class PendingDeletion:
    """What an operator has to be told while a deletion is being confirmed."""

    source_id: str
    object_count: int
    indexed_count: int
    confirmations: int
    required: int
    first_seen_at: datetime | None
    last_seen_at: datetime | None

    def payload(self) -> dict:
        return {
            "object_count": self.object_count,
            "indexed_count": self.indexed_count,
            "confirmations": self.confirmations,
            "required": self.required,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


def record(
    session: Session,
    source_id: str,
    external_ids: set[str],
    *,
    required: int,
    indexed_count: int,
) -> PendingDeletion:
    """Register what this scan found missing and report how often it has been seen.

    Returns the state *after* this scan: ``confirmations == 1`` for a set nobody has seen
    before or one that differs from what was held, incremented only when the set matches
    exactly. Writes on the caller's session, so a scan whose transaction is rolled back
    leaves no half-confirmed deletion behind.
    """
    now = datetime.now(UTC)
    watch = session.scalar(
        select(SourceDeletionWatch).where(SourceDeletionWatch.source_id == source_id)
    )
    if watch is None:
        watch = SourceDeletionWatch(
            source_id=source_id,
            confirmations=1,
            required=required,
            object_count=len(external_ids),
            indexed_count=indexed_count,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(watch)
        session.flush()
        _replace_candidates(session, watch.id, external_ids)
    else:
        if _candidates(session, watch.id) == external_ids:
            watch.confirmations += 1
        else:
            # Different objects are missing now, so nothing has been confirmed: this is a
            # new claim starting from one, not a continuation of the old one.
            watch.confirmations = 1
            watch.first_seen_at = now
            _replace_candidates(session, watch.id, external_ids)
        watch.object_count = len(external_ids)
        watch.indexed_count = indexed_count
        # Read from the live policy every scan: raising the threshold has to take effect
        # on the next sync, not on the next deletion.
        watch.required = required
        watch.last_seen_at = now
    session.flush()
    return _state(watch)


def clear(session: Session, source_id: str) -> None:
    """Forget any held deletion for this source (it was applied, or is no longer true)."""
    watch = session.scalar(
        select(SourceDeletionWatch).where(SourceDeletionWatch.source_id == source_id)
    )
    if watch is None:
        return
    # Explicit rather than relying on ON DELETE CASCADE: SQLite does not enforce foreign
    # keys unless asked to, and orphaned candidate rows would be compared against a later
    # scan as though they meant something.
    session.execute(
        delete(SourceDeletionCandidate).where(SourceDeletionCandidate.watch_id == watch.id)
    )
    session.delete(watch)
    session.flush()


def pending(session: Session, source_id: str) -> PendingDeletion | None:
    watch = session.scalar(
        select(SourceDeletionWatch).where(SourceDeletionWatch.source_id == source_id)
    )
    return _state(watch) if watch is not None else None


def pending_external_ids(session: Session, source_id: str) -> set[str]:
    """The exact set being confirmed, for callers that need the objects themselves."""
    watch = session.scalar(
        select(SourceDeletionWatch).where(SourceDeletionWatch.source_id == source_id)
    )
    return _candidates(session, watch.id) if watch is not None else set()


def _candidates(session: Session, watch_id: str) -> set[str]:
    return set(
        session.scalars(
            select(SourceDeletionCandidate.external_id).where(
                SourceDeletionCandidate.watch_id == watch_id
            )
        ).all()
    )


def _replace_candidates(session: Session, watch_id: str, external_ids: set[str]) -> None:
    session.execute(
        delete(SourceDeletionCandidate).where(SourceDeletionCandidate.watch_id == watch_id)
    )
    if not external_ids:
        return
    session.execute(
        insert(SourceDeletionCandidate),
        [
            {"watch_id": watch_id, "external_id": external_id}
            for external_id in sorted(external_ids)
        ],
    )


def _state(watch: SourceDeletionWatch) -> PendingDeletion:
    return PendingDeletion(
        source_id=watch.source_id,
        object_count=watch.object_count,
        indexed_count=watch.indexed_count,
        confirmations=watch.confirmations,
        required=watch.required,
        first_seen_at=watch.first_seen_at,
        last_seen_at=watch.last_seen_at,
    )
