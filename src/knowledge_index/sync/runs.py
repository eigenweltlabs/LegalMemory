"""Source synchronization as an orchestrated run rather than an HTTP request.

Scanning a firm's DMS is minutes to hours of network I/O against a system this
appliance does not control. Doing it inside the request that asked for it means any
proxy in front of the app decides how long a firm's first sync may take, a closed tab
loses the outcome, and a second click starts a second concurrent scan of the same
estate. So the request only *reserves* the work: it writes one ``pipeline_runs`` row per
source and hands it to the configured orchestrator. Everything after that — progress,
failure, the handoff to insertion — is recorded on that row, which is the same row the
insertion pipeline uses and the same one ``/api/runs`` already serves.

The reservation is also what prevents overlapping scans: the row exists before any
connector is built, and a source that already has an unfinished sync run cannot get a
second one. See ``uq_pipeline_runs_active_sync`` for the database-level backstop.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun as PipelineRunRecord, Source
from knowledge_index.pipeline.runner import connector_from_source
from knowledge_index.sync.engine import SyncEngine, SyncResult

WORKFLOW = "source-sync"
ACTIVE_STATUSES = ("queued", "running")
# A source in these states has no usable connector: paused is an operator instruction,
# pending_auth means the provider has not answered the OAuth handshake yet.
NON_SYNCABLE_STATUSES = {"paused": "source is paused", "pending_auth": "awaiting authorization"}
# How often a running scan publishes its observation count. Frequent enough that an
# operator sees a number move within seconds of clicking, rare enough that a 500k-object
# estate does not spend its time writing progress rows.
PROGRESS_EVERY = 50


def _log(message: str) -> None:
    print(f"[ki sync] {message}", file=sys.stderr, flush=True)


class UnknownSource(LookupError):
    """The requested source id does not exist."""


class SyncRunFailed(RuntimeError):
    """A reserved sync run ended in ``failed``; the cause is recorded on the run row."""


@dataclass(frozen=True)
class EnqueuedRun:
    run_id: str
    source_id: str
    display_name: str

    def payload(self) -> dict:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class SkippedSource:
    source_id: str
    display_name: str
    reason: str

    def payload(self) -> dict:
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "reason": self.reason,
        }


@dataclass
class SyncEnqueueResult:
    runs: list[EnqueuedRun] = field(default_factory=list)
    skipped: list[SkippedSource] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "runs": [run.payload() for run in self.runs],
            "skipped": [item.payload() for item in self.skipped],
        }


# --------------------------------------------------------------------------- enqueue


def enqueue_sync(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    source_id: str | None = None,
    source_ids: set[str] | None = None,
    trigger: str = "api",
) -> SyncEnqueueResult:
    """Reserve and dispatch one sync run per eligible source. Never scans inline.

    ``source_id`` targets exactly one source and raises ``UnknownSource`` if it does not
    exist. ``source_ids`` is the watcher's form: the sources whose roots changed. Neither
    means every source that is currently syncable.

    ``trigger`` is recorded on the run. The watcher's safety-net reconcile produces a run
    on every interval whether or not anything changed, which is correct — a scan
    happened, so there is a run — but it means an operator looking for the sync they
    started by hand needs to be able to tell the two apart.
    """
    result = SyncEnqueueResult()
    provider = config.components.orchestrator_provider
    for candidate_id, display_name, status in _candidates(
        session_factory, source_id=source_id, source_ids=source_ids
    ):
        blocked = NON_SYNCABLE_STATUSES.get(status)
        if blocked is not None:
            result.skipped.append(SkippedSource(candidate_id, display_name, blocked))
            continue
        run_id, reason = _reserve_run(session_factory, candidate_id, provider, trigger)
        if run_id is None:
            result.skipped.append(SkippedSource(candidate_id, display_name, reason or "not eligible"))
            continue
        result.runs.append(EnqueuedRun(run_id, candidate_id, display_name))
        _dispatch(session_factory, config, run_id, candidate_id)
    return result


def _candidates(
    session_factory: sessionmaker[Session],
    *,
    source_id: str | None,
    source_ids: set[str] | None,
) -> list[tuple[str, str, str]]:
    with session_factory() as session:
        if source_id is not None:
            source = session.get(Source, source_id)
            if source is None:
                raise UnknownSource(source_id)
            return [(source.id, source.display_name, source.status)]
        statement = select(Source).order_by(Source.display_name, Source.id)
        if source_ids is not None:
            if not source_ids:
                return []
            statement = statement.where(Source.id.in_(source_ids))
        return [(row.id, row.display_name, row.status) for row in session.scalars(statement)]


def _reserve_run(
    session_factory: sessionmaker[Session], source_id: str, provider: str, trigger: str
) -> tuple[str | None, str | None]:
    """Claim the right to sync one source, or report why it is already claimed."""
    with session_factory() as session:
        # Serialize the look-then-insert for this source. Without it two requests that
        # arrive together both see no active run and both insert one.
        _advisory_xact_lock(session, f"source-sync:{source_id}")
        source = session.get(Source, source_id)
        if source is None:
            return None, "source no longer exists"
        active = session.scalar(
            select(PipelineRunRecord.id)
            .where(
                PipelineRunRecord.source_id == source_id,
                PipelineRunRecord.workflow == WORKFLOW,
                PipelineRunRecord.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if active is not None:
            return None, f"a sync is already in flight for this source (run {active})"
        record = PipelineRunRecord(
            project_id=source.project_id,
            source_id=source_id,
            provider=provider,
            workflow=WORKFLOW,
            status="queued",
            progress=0,
            current_step="queued",
            counters=_initial_counters(source, trigger),
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            # The partial unique index caught what the advisory lock could not — another
            # process on another connection, or a lock this dialect does not implement.
            session.rollback()
            return None, "a sync is already in flight for this source"
        return record.id, None


def _initial_counters(source: Source, trigger: str) -> dict:
    return {
        "source_display_name": source.display_name,
        "trigger": trigger,
        "observed": 0,
        "created": 0,
        "changed": 0,
        "metadata_changed": 0,
        "access_changed": 0,
        "unchanged": 0,
        "restored": 0,
        "tombstoned": 0,
        # Objects a scan believes are deleted but has not removed yet, pending
        # confirmation by later scans (see sync/deletions.py).
        "pending_deletions": 0,
        "batches": 0,
        # Unknown until the engine decides between a delta feed and a full scan.
        "mode": None,
        "insertion_run_id": None,
    }


def _dispatch(
    session_factory: sessionmaker[Session], config: AppConfig, run_id: str, source_id: str
) -> None:
    if config.components.orchestrator_provider == "hatchet":
        from knowledge_index.orchestration.hatchet import trigger_source_sync

        try:
            provider_run_id = trigger_source_sync(session_factory, config, run_id, source_id)
        except Exception as exc:
            # The reservation is already durable, so a trigger that never landed has to
            # be closed out here or the source stays blocked by a run nobody will finish.
            _fail_run(session_factory, run_id, exc, step="dispatch")
            _log(f"run {run_id}: hatchet dispatch failed: {type(exc).__name__}: {exc}")
            return
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is not None:
                record.provider_run_id = provider_run_id
                session.commit()
        return
    _submit_local(lambda: execute_sync_run(session_factory, config, run_id))


# ------------------------------------------------------------------- in-process runner

_LOCAL_POOL: ThreadPoolExecutor | None = None
_LOCAL_FUTURES: list[Future] = []


def _submit_local(work: Callable[[], None]) -> None:
    """Run a reserved sync off the caller's thread on the single-VM deployment.

    ``orchestrator_provider = "local"`` is a supported deployment, not a test double, so
    it gets the same contract as Hatchet: the request returns once the run is reserved
    and the run row is what reports progress.
    """
    global _LOCAL_POOL
    if _LOCAL_POOL is None:
        _LOCAL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ki-sync")
    future = _LOCAL_POOL.submit(work)
    _LOCAL_FUTURES.append(future)
    # Bounded: this list exists to let an operator command or a test wait for in-flight
    # work, not to be a run history — that is what pipeline_runs is for.
    del _LOCAL_FUTURES[:-64]


def wait_for_local_runs(timeout: float = 300.0) -> None:
    """Block until in-process sync runs started by this process have finished."""
    deadline = datetime.now(UTC).timestamp() + timeout
    for future in list(_LOCAL_FUTURES):
        remaining = deadline - datetime.now(UTC).timestamp()
        if remaining <= 0:
            raise TimeoutError("in-process sync runs did not finish in time")
        future.result(timeout=remaining)


def wait_for_run(
    session_factory: sessionmaker[Session], run_id: str, *, timeout: float = 86400.0
) -> dict:
    """Poll one sync run to a terminal state. For callers that are humans at a terminal.

    Watching the run row rather than the future, so it works the same whether the scan
    is on this process's thread pool or on a Hatchet worker in another container.
    """
    deadline = time.monotonic() + timeout
    while True:
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is None:
                raise SyncRunFailed(f"sync run disappeared: {run_id}")
            snapshot = {
                "run_id": record.id,
                "source_id": record.source_id,
                "status": record.status,
                "current_step": record.current_step,
                "counters": dict(record.counters or {}),
                "error": record.last_error,
            }
        if snapshot["status"] in ("completed", "failed"):
            return snapshot
        if time.monotonic() > deadline:
            raise TimeoutError(f"sync run {run_id} did not finish within {timeout}s")
        time.sleep(1.0)


# ------------------------------------------------------------------------- run stages


def execute_sync_run(
    session_factory: sessionmaker[Session], config: AppConfig, run_id: str
) -> None:
    """Scan, then hand off. The whole run, for orchestrators without a task DAG."""
    try:
        run_scan(session_factory, config, run_id)
    except SyncRunFailed:
        # Already recorded on the run row, and there is nothing to hand off.
        return
    run_handoff(session_factory, config, run_id)


def run_scan(session_factory: sessionmaker[Session], config: AppConfig, run_id: str) -> SyncResult:
    """Run one source's scan and record the outcome on its run row.

    Raises ``SyncRunFailed`` after marking the run failed, so an orchestrator sees the
    task fail and does not schedule the handoff behind a scan that did not happen.
    """
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise SyncRunFailed(f"sync run does not exist: {run_id}")
        if record.status not in ACTIVE_STATUSES:
            raise SyncRunFailed(f"sync run {run_id} is already {record.status}")
        source_id = record.source_id
        if source_id is None:
            raise SyncRunFailed(f"sync run {run_id} has no source")
        record.status = "running"
        record.current_step = "scan"
        record.started_at = record.started_at or datetime.now(UTC)
        session.commit()

    with session_factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            _fail_run(
                session_factory,
                run_id,
                LookupError(f"source {source_id} was removed before its sync started"),
                step="scan",
            )
            raise SyncRunFailed(f"source {source_id} no longer exists")
        try:
            connector = connector_from_source(source, session)
            result = SyncEngine(
                session,
                source,
                connector,
                selection_fingerprint=_selection_fingerprint(source),
                acl_refresh_hours=config.security.acl_refresh_hours,
                deletion_confirmations=config.pipeline.deletion_confirmations,
                on_progress=_progress_writer(session_factory, run_id),
            ).sync()
        except Exception as exc:
            # Keep whatever the scan did observe and mark the source for an operator.
            # The engine never tombstones without reaching EOF, so a half-finished scan
            # can only have added or updated rows, never removed any.
            _mark_source_error(session_factory, session, source_id)
            _fail_run(session_factory, run_id, exc, step="scan")
            _log(f"run {run_id}: sync failed for source {source_id}: {type(exc).__name__}: {exc}")
            raise SyncRunFailed(str(exc)) from exc
        session.commit()

    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is not None:
            counters = dict(record.counters or {})
            counters.update(result.__dict__)
            record.counters = counters
            record.current_step = "handoff"
            session.commit()
    return result


def run_handoff(
    session_factory: sessionmaker[Session], config: AppConfig, run_id: str
) -> str | None:
    """Finish a scanned run: start insertion if it is wanted, then complete the run."""
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise SyncRunFailed(f"sync run does not exist: {run_id}")
        counters = dict(record.counters or {})
    insertion_changes = any(
        counters.get(key)
        for key in ("created", "changed", "access_changed", "restored")
    )
    if not config.pipeline.auto_insert_after_sync:
        _complete_run(session_factory, run_id, step="complete (insertion not automatic)")
        return None
    if not insertion_changes:
        if counters.get("tombstoned"):
            # A tombstone becomes inaccessible in the scan transaction itself:
            # AccessService requires a live SourceObject on every retrieval path.
            # Tombstones are deliberately retained so a restored provider object can
            # reconnect to its existing version and cached artifacts. Launching the
            # global insertion pipeline here cannot process deleted objects (and used
            # to relaunch unrelated documents that another insertion was already
            # handling).
            _complete_run(session_factory, run_id, step="complete (deletions applied)")
            return None
        _complete_run(session_factory, run_id, step="complete (nothing new to insert)")
        return None

    from knowledge_index.orchestration.insertion import launch_insertion

    try:
        launched = launch_insertion(session_factory, config)
    except Exception as exc:
        # The scan itself succeeded and its counters stand, but the documents it found
        # will not be indexed until someone acts, so this run is not a success.
        _fail_run(session_factory, run_id, exc, step="handoff")
        _log(f"run {run_id}: insertion handoff failed: {type(exc).__name__}: {exc}")
        raise SyncRunFailed(f"insertion handoff failed: {exc}") from exc
    insertion_run_id = launched["run_id"]
    _complete_run(session_factory, run_id, step="complete", insertion_run_id=insertion_run_id)
    return insertion_run_id


# ------------------------------------------------------------------------- run record


def _progress_writer(
    session_factory: sessionmaker[Session], run_id: str
) -> Callable[[SyncResult], None]:
    """Publish the live observation count on a connection of its own.

    Deliberately not the engine's session: that transaction holds the scan's writes and
    must not be committed early, and an operator watching a first sync needs the number
    before it ends.
    """

    def publish(result: SyncResult) -> None:
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is None:
                return
            counters = dict(record.counters or {})
            counters.update(result.__dict__)
            record.counters = counters
            # No fabricated fraction: a scan has no denominator until it reaches the end
            # of the estate, so the honest live signal is the count, not a bar.
            record.current_step = f"scan ({result.observed} observed)"
            session.commit()

    return publish


def _complete_run(
    session_factory: sessionmaker[Session],
    run_id: str,
    *,
    step: str,
    insertion_run_id: str | None = None,
) -> None:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            return
        counters = dict(record.counters or {})
        counters["insertion_run_id"] = insertion_run_id
        record.counters = counters
        record.status = "completed"
        record.progress = 1
        record.current_step = step
        record.finished_at = record.finished_at or datetime.now(UTC)
        session.commit()


def _fail_run(
    session_factory: sessionmaker[Session], run_id: str, exc: BaseException, *, step: str
) -> None:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            return
        record.status = "failed"
        record.current_step = step
        record.last_error = {"class": type(exc).__name__, "message": str(exc)}
        record.finished_at = datetime.now(UTC)
        session.commit()


def _mark_source_error(
    session_factory: sessionmaker[Session], session: Session, source_id: str
) -> None:
    """Persist ``error`` on the source, even if the scan left its session unusable."""
    try:
        source = session.get(Source, source_id)
        if source is not None:
            source.status = "error"
        session.commit()
        return
    except Exception:
        session.rollback()
    with session_factory() as repair:
        source = repair.get(Source, source_id)
        if source is not None:
            source.status = "error"
            repair.commit()


def _selection_fingerprint(source: Source) -> str:
    """Digest of the folder selection this sync is running under."""
    from knowledge_index.connectors import scoping

    return scoping.fingerprint((source.config or {}).get("connector"))


def _advisory_xact_lock(session: Session, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))
