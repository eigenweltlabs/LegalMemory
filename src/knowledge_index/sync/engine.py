"""Persistence-aware synchronization around deliberately small connectors."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.db.models import ProcessingState, Source, SourceObject
from knowledge_index.permissions import replace_source_object_grants
from knowledge_index.sync import deletions
from knowledge_index.sync.base import SourceObjectObservation, SyncSource, UnsupportedOperation
from knowledge_index.taxonomies import (
    ACCESS_ONLY_REINDEX,
    PIPELINE_STAGE_ORDER,
    WAITING_FOR_PREVIOUS_STAGE,
    PipelineStage,
    ProcessingStatus,
)

# How many consecutive scans have to report the identical set of missing objects before a
# deletion too large to trust from one scan is applied. See sync/deletions.py.
DEFAULT_DELETION_CONFIRMATIONS = 3


@dataclass
class SyncResult:
    observed: int = 0
    created: int = 0
    # Byte changes that require the complete insertion pipeline.
    changed: int = 0
    # The bytes were identical, although source metadata such as path, mtime or etag
    # changed. Kept separate so metadata churn never spends model or embedding budget.
    metadata_changed: int = 0
    # Mirrored grants changed while the bytes stayed identical. This queues only the
    # lightweight access projection in the index stage.
    access_changed: int = 0
    unchanged: int = 0
    restored: int = 0
    tombstoned: int = 0
    batches: int = 0
    mode: str = "full"
    # A deletion this scan believes in but has not applied yet, and how far through
    # confirmation it is. Reported on the run and on the source so nobody has to guess why
    # documents that are gone at the source are still searchable here.
    pending_deletions: int = 0
    deletion_confirmations: int = 0
    deletion_confirmations_required: int = 0


class SyncAborted(RuntimeError):
    """A scan that cannot be acted on at all.

    The tombstone guards no longer raise this: a deletion too large to trust from one
    scan is confirmed across syncs instead of refused, because refusing left a real bulk
    deletion applyable only from psql. Kept as the engine's "this scan is not usable"
    signal for callers that raise it.
    """


class SyncEngine:
    """Apply connector observations atomically to the source and pipeline ledgers."""

    # A scan that returns nothing for a source that previously had content is almost
    # never a genuinely emptied estate. It is a revoked scope, an expired licence, or a
    # connector swallowing an API error and exiting cleanly. Tombstoning on that signal
    # deletes a firm's entire index from a sync that reported success.
    #
    # The fraction guard covers the partial version of the same failure: a connector that
    # can enumerate one site out of forty should not tombstone the other thirty-nine.
    #
    # Crossing either threshold no longer refuses the scan. It moves the deletion into
    # confirmation (sync/deletions.py): the objects stay indexed, the set of missing ids
    # is recorded, and the tombstones are applied once that identical set has come back
    # from `deletion_confirmations` consecutive scans. Refusing instead meant a firm that
    # really did delete a matter could only get it out of the index from psql.
    MAX_TOMBSTONE_FRACTION = 0.5
    MIN_OBJECTS_FOR_FRACTION_GUARD = 20

    # How many observations pass between progress callbacks. A scan of a real estate is
    # minutes of network I/O; without this the only signal an operator gets is "still
    # running", which is indistinguishable from a hung connector.
    PROGRESS_EVERY = 50

    def __init__(
        self,
        session: Session,
        source_record: Source,
        connector: SyncSource,
        *,
        acl_refresh_hours: int = 24,
        selection_fingerprint: str | None = None,
        deletion_confirmations: int = DEFAULT_DELETION_CONFIRMATIONS,
        on_progress: Callable[[SyncResult], None] | None = None,
    ) -> None:
        if source_record.kind != connector.kind:
            raise ValueError(
                f"source kind {source_record.kind!r} does not match connector {connector.kind!r}"
            )
        self.session = session
        self.source_record = source_record
        self.connector = connector
        self.acl_refresh_hours = acl_refresh_hours
        self.selection_fingerprint = selection_fingerprint
        self.deletion_confirmations = max(1, int(deletion_confirmations))
        self.on_progress = on_progress

    def sync(self, *, force_full: bool = False) -> SyncResult:
        # A delta token describes changes within the old selection, so resuming from it
        # after a re-scope would never enumerate the newly included folders.
        if self._rescoped():
            force_full = True
        # A deletion waiting for confirmation can only be confirmed by another crawl of
        # the whole estate: a delta feed reports what changed, and objects that are simply
        # still absent change nothing. Without this the held set would sit unresolved
        # until the ACL refresh forced a full scan hours later, with the documents
        # searchable and the operator told "confirming" for days.
        if deletions.pending(self.session, self.source_record.id) is not None:
            force_full = True
        if self.connector.capabilities.delta and not (force_full or self._acls_are_stale()):
            try:
                result = self._incremental_sync()
                # A person added to or removed from a source group keeps the wrong
                # access until the memberships are re-mirrored. Connectors whose group
                # expansion is cheap refresh them on every sync, so the policy interval
                # — not the daily full refresh — bounds that window.
                if getattr(self.connector, "cheap_memberships", False):
                    self._refresh_memberships()
                return result
            except UnsupportedOperation:
                pass
        result = self._full_sync()
        self._refresh_memberships()
        return result

    def _acls_are_stale(self) -> bool:
        """Whether a full scan is due purely to re-read permissions.

        A delta feed reports content changes. Revoking someone's access changes no
        document, so a source synced only incrementally keeps serving grants that the
        source has already withdrawn. Periodically forcing a full scan bounds how long
        that window can be.
        """
        if self.acl_refresh_hours <= 0 or not self.connector.capabilities.acl:
            return False
        last_full = self.source_record.last_full_sync_at
        if last_full is None:
            return True
        if last_full.tzinfo is None:
            last_full = last_full.replace(tzinfo=UTC)
        return datetime.now(UTC) - last_full >= timedelta(hours=self.acl_refresh_hours)

    def _refresh_memberships(self) -> None:
        """Mirror the source's group memberships after a successful full scan.

        Without these, a grant to a source group matches no caller and the documents are
        invisible rather than protected.
        """
        collect = getattr(self.connector, "memberships", None)
        if collect is None:
            return
        memberships = collect()
        from knowledge_index.connectors.principals import replace_memberships

        # Empty is a real snapshot: the last member may have been removed, or the source
        # may have lost permission to enumerate the group. Keeping the old rows in either
        # case preserves access the source no longer proves. Replacing them with none is
        # the fail-closed answer and lets a later healthy scan restore the current set.
        replace_memberships(self.session, self.source_record.id, memberships)

    def _rescoped(self) -> bool:
        """Whether the folder selection changed since the last sync.

        A re-scope is a deliberate instruction: an operator narrowing the synced folders
        means the objects outside them should leave the index. That is the one case where
        a large drop in observed objects is expected rather than alarming, so it lifts the
        tombstone guards for exactly this run.

        Both fingerprints have to be known for that instruction to exist. A source with
        none recorded has not been re-scoped — nobody has changed anything — so the
        guards stay on and this run records the fingerprint. Reading a missing value as a
        change would instead lift the guards on a source's first sync, which is where an
        empty result is most likely to mean a credential that never worked rather than a
        folder that is genuinely gone.
        """
        return (
            self.selection_fingerprint is not None
            and self.source_record.selection_fingerprint is not None
            and self.selection_fingerprint != self.source_record.selection_fingerprint
        )

    def _tombstone_policy(self) -> tuple[bool, float, int]:
        """``(allow_empty_scan, max_tombstone_fraction, confirmations)`` for this source."""
        policy = self.source_record.sync_policy or {}
        return (
            bool(policy.get("allow_empty_scan", False)) or self._rescoped(),
            float(policy.get("max_tombstone_fraction", self.MAX_TOMBSTONE_FRACTION)),
            max(1, int(policy.get("deletion_confirmations", self.deletion_confirmations))),
        )

    def _needs_confirmation(self, *, observed: int, missing: int, existing: int) -> bool:
        """Whether this deletion is too large to trust from a single scan."""
        allow_empty, max_fraction, _ = self._tombstone_policy()
        if existing == 0 or missing == 0:
            return False
        if self.connector.capabilities.verifiable_emptiness:
            # A mounted folder or drop directory can prove the objects are gone: the
            # listing IS the estate, so there is nothing to confirm against.
            return False
        if allow_empty:
            # Either the operator declared this source may empty itself, or the folder
            # selection changed — an explicit instruction, not an observation.
            return False
        if observed == 0:
            # Nothing came back at all. The most suspicious shape there is, so it never
            # applies on the strength of one scan regardless of how small the source is.
            return True
        return existing >= self.MIN_OBJECTS_FOR_FRACTION_GUARD and (
            missing / existing > max_fraction
        )

    def _hold_deletion(
        self, result: SyncResult, *, missing: list[SourceObject], existing: int
    ) -> bool:
        """Decide whether to tombstone now, recording progress towards confirmation.

        Returns ``True`` when the tombstones must wait for a later scan to agree. The
        objects stay live and searchable meanwhile, which is exactly why the count and the
        confirmation progress are put on the result: this is the state an operator must be
        told about rather than left to discover.
        """
        if not self._needs_confirmation(
            observed=result.observed, missing=len(missing), existing=existing
        ):
            # This scan is trustworthy, so whatever was being confirmed is stale: either
            # the objects came back or a different, smaller set is missing now.
            deletions.clear(self.session, self.source_record.id)
            return False

        _allow_empty, _fraction, required = self._tombstone_policy()
        state = deletions.record(
            self.session,
            self.source_record.id,
            {row.external_id for row in missing},
            required=required,
            indexed_count=existing,
        )
        result.deletion_confirmations = state.confirmations
        result.deletion_confirmations_required = state.required
        if state.confirmations < state.required:
            result.pending_deletions = state.object_count
            return True
        # Every scan since the first reported this identical set. Two independent crawls
        # of the estate agreeing on which objects are gone is the evidence one crawl could
        # not provide, so the deletion is applied and the claim closed.
        deletions.clear(self.session, self.source_record.id)
        return False

    def _full_sync(self) -> SyncResult:
        result = SyncResult(mode="full", batches=1)
        seen: set[str] = set()

        # Do not tombstone anything unless iteration reaches EOF. A connector crash in
        # the middle of a crawl must leave the previous index intact.
        for observation in self.connector.full_scan():
            if observation.external_id in seen:
                raise ValueError(
                    f"connector emitted duplicate external id: {observation.external_id}"
                )
            seen.add(observation.external_id)
            self._apply_observation(observation, result)

        existing = self.session.scalars(
            select(SourceObject).where(
                SourceObject.source_id == self.source_record.id,
                SourceObject.deleted_at.is_(None),
            )
        ).all()
        missing = [row for row in existing if row.external_id not in seen]
        held = self._hold_deletion(result, missing=missing, existing=len(existing))

        now = datetime.now(UTC)
        if not held:
            for source_object in missing:
                source_object.deleted_at = now
                result.tombstoned += 1

        cursor_state = getattr(self.connector, "cursor_state", None)
        # Provider connectors seed the token for their next delta run while completing a
        # full crawl. Persist it here; clearing it made continuous Drive and SharePoint
        # policies perform a full estate crawl on every interval.
        self.source_record.cursor = cursor_state() if callable(cursor_state) else None
        self.source_record.last_full_sync_at = now
        if self.selection_fingerprint is not None:
            self.source_record.selection_fingerprint = self.selection_fingerprint
        self.source_record.status = "active"
        self.source_record.last_sync_at = datetime.now(UTC)
        self.source_record.last_sync_summary = result.__dict__.copy()
        self.session.flush()
        return result

    def _incremental_sync(self) -> SyncResult:
        result = SyncResult(mode="incremental")
        cursor = self.source_record.cursor

        while True:
            batch = self.connector.changes(cursor)
            result.batches += 1
            for observation in batch.observations:
                self._apply_observation(observation, result)
            result.tombstoned += self._apply_deletions(batch.deleted_external_ids)

            cursor = batch.next_cursor
            self.source_record.cursor = cursor
            self.source_record.status = "active"
            self.session.flush()
            if not batch.has_more:
                break
        # Recorded here as well as after a full scan. A source that only ever syncs
        # incrementally still needs a known fingerprint for a later re-scope to differ
        # from.
        if self.selection_fingerprint is not None:
            self.source_record.selection_fingerprint = self.selection_fingerprint
        self.source_record.last_sync_at = datetime.now(UTC)
        self.source_record.last_sync_summary = result.__dict__.copy()
        self.session.flush()
        return result

    def _apply_observation(
        self, observation: SourceObjectObservation, result: SyncResult
    ) -> SourceObject:
        if not observation.external_id:
            raise ValueError("connector observation has an empty external id")
        result.observed += 1
        if self.on_progress is not None and result.observed % self.PROGRESS_EVERY == 0:
            self.on_progress(result)
        source_object = self.session.scalar(
            select(SourceObject).where(
                SourceObject.source_id == self.source_record.id,
                SourceObject.external_id == observation.external_id,
            )
        )
        now = datetime.now(UTC)
        if source_object is None:
            source_object = SourceObject(
                source_id=self.source_record.id,
                external_id=observation.external_id,
                path=observation.path,
                name=observation.name,
            )
            self.session.add(source_object)
            self._copy_observation(source_object, observation)
            self.session.flush()
            replace_source_object_grants(
                self.session, source_object.id, observation.acl, source_id=self.source_record.id
            )
            self._ensure_pipeline(source_object, reset=True)
            result.created += 1
            return source_object

        restored = source_object.deleted_at is not None
        metadata_changed = self._observation_changed(source_object, observation)
        previous_content_hash = source_object.content_hash
        staged_content_hash = self._staged_content_hash(observation.staged_path)
        content_change_proven = bool(
            previous_content_hash
            and staged_content_hash
            and staged_content_hash != previous_content_hash
        )
        content_unchanged_proven = bool(
            previous_content_hash
            and staged_content_hash
            and staged_content_hash == previous_content_hash
        )
        self._copy_observation(source_object, observation)
        access_changed = replace_source_object_grants(
            self.session, source_object.id, observation.acl, source_id=self.source_record.id
        )
        source_object.last_seen = now
        source_object.deleted_at = None

        if restored:
            source_object.content_hash = None
            self._ensure_pipeline(source_object, reset=True)
            result.restored += 1
        elif content_change_proven or (metadata_changed and not content_unchanged_proven):
            # The source reported metadata churn and the staged bytes do not prove the
            # old content hash still holds. Fail safe: fetch and derive everything again.
            source_object.content_hash = None
            self._ensure_pipeline(source_object, reset=True)
            result.changed += 1
        else:
            self._ensure_pipeline(source_object, reset=False)
            if metadata_changed:
                result.metadata_changed += 1
            if access_changed:
                self._queue_access_reindex(source_object)
                result.access_changed += 1
            result.unchanged += 1
        return source_object

    @staticmethod
    def _staged_content_hash(staged_path: str | None) -> str | None:
        """Hash connector-staged bytes before deciding which work to invalidate.

        Provider etags and mtimes are not content identities: SharePoint and Drive both
        change them for renames, sharing and other metadata operations. Connectors stage
        API content before yielding an observation, so hashing those bytes against the
        already indexed SHA-256 is the provider-neutral boundary between "refresh
        metadata/access" and "derive the document again".

        If the staged file is unavailable we return None and metadata changes retain the
        old conservative full-pipeline behaviour.
        """
        if not staged_path:
            return None
        path = Path(staged_path)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return None
        return digest.hexdigest()

    @staticmethod
    def _observation_changed(
        source_object: SourceObject, observation: SourceObjectObservation
    ) -> bool:
        return any(
            (
                source_object.path != observation.path,
                source_object.name != observation.name,
                source_object.size_bytes != observation.size_bytes,
                not _same_instant(source_object.mtime, observation.mtime),
                source_object.source_version_label != observation.source_version_label,
            )
        )

    @staticmethod
    def _copy_observation(
        source_object: SourceObject, observation: SourceObjectObservation
    ) -> None:
        source_object.path = observation.path
        source_object.name = observation.name
        source_object.container = str(observation.path.rpartition("/")[0]) or None
        source_object.mime_type = observation.mime_type
        source_object.size_bytes = observation.size_bytes
        source_object.mtime = observation.mtime
        source_object.author_hint = observation.author_hint
        source_object.source_version_label = observation.source_version_label
        source_object.acl = observation.acl
        if observation.staged_path:
            source_object.staged_path = observation.staged_path

    def _ensure_pipeline(self, source_object: SourceObject, *, reset: bool) -> None:
        states = {
            state.stage: state
            for state in self.session.scalars(
                select(ProcessingState).where(ProcessingState.source_object_id == source_object.id)
            ).all()
        }
        for stage in PIPELINE_STAGE_ORDER:
            state = states.get(stage.value)
            if state is None:
                self.session.add(
                    ProcessingState(
                        source_object_id=source_object.id,
                        stage=stage.value,
                        status=(
                            ProcessingStatus.PENDING.value
                            if stage == PipelineStage.FETCH
                            else ProcessingStatus.SKIPPED.value
                        ),
                        last_error=(
                            None
                            if stage == PipelineStage.FETCH
                            else {"reason": WAITING_FOR_PREVIOUS_STAGE}
                        ),
                    )
                )
            elif reset:
                state.status = (
                    ProcessingStatus.PENDING.value
                    if stage == PipelineStage.FETCH
                    else ProcessingStatus.SKIPPED.value
                )
                state.attempts = 0
                state.next_retry_at = None
                state.claimed_at = None
                state.last_error = (
                    None
                    if stage == PipelineStage.FETCH
                    else {"reason": WAITING_FOR_PREVIOUS_STAGE}
                )

    def _queue_access_reindex(self, source_object: SourceObject) -> None:
        """Queue only the index access projection when upstream work is settled.

        If content processing is already pending or running, its eventual index stage
        will read the new grants, so replacing that work with an access-only claim would
        be incorrect. A settled index row can safely be reopened with a durable marker
        that tells the handler to preserve chunks and embeddings.
        """
        states = {
            state.stage: state
            for state in self.session.scalars(
                select(ProcessingState).where(
                    ProcessingState.source_object_id == source_object.id
                )
            ).all()
        }
        index_state = states.get(PipelineStage.INDEX.value)
        if index_state is None:
            return
        if index_state.status not in {
            ProcessingStatus.DONE.value,
            ProcessingStatus.SKIPPED.value,
        }:
            return
        if (
            index_state.status == ProcessingStatus.SKIPPED.value
            and (index_state.last_error or {}).get("reason") == WAITING_FOR_PREVIOUS_STAGE
        ):
            return
        index_state.status = ProcessingStatus.PENDING.value
        index_state.attempts = 0
        index_state.next_retry_at = None
        index_state.claimed_at = None
        index_state.last_error = {"reason": ACCESS_ONLY_REINDEX}
        index_state.producer_version = None

    def _apply_deletions(self, external_ids: list[str]) -> int:
        if not external_ids:
            return 0
        rows = self.session.scalars(
            select(SourceObject).where(
                SourceObject.source_id == self.source_record.id,
                SourceObject.external_id.in_(external_ids),
                SourceObject.deleted_at.is_(None),
            )
        ).all()
        now = datetime.now(UTC)
        for source_object in rows:
            source_object.deleted_at = now
        return len(rows)


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    """Compare DB timestamps across drivers that may drop the UTC tzinfo (SQLite)."""
    if left is None or right is None:
        return left is right
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    return left == right
