"""Interval scheduling for every source, whatever kind it is.

``sync_policy = {"mode": "continuous", "interval": "2m"}`` used to be honoured only by
the folder watcher, and only for ``local_fs`` / ``plugin_drop``. A SharePoint, OneDrive,
Google Drive, Slack or Gmail connection carrying the same policy was stored, displayed as
"continuous" in the admin UI, and never synced by anything: the estate went stale and the
product said it was current. This is the one scheduler, and it does not care about kind.

**Why one scheduler and not two.** The folder watcher exists because a mounted filesystem
can *tell* us a file changed, which no API-backed connector can; it stays, and it stays
event-driven, enqueuing within a second of a write. What it no longer does is drive the
interval, because two components enqueuing the same local folder on two timers means two
sets of logs and two ways to answer "why did this sync". Latency is the watcher's job,
the timetable is this module's, and both hand the work to the same
:func:`knowledge_index.sync.runs.enqueue_sync`.

**Where it runs.** In the app process (``ki serve``), started by the CLI rather than by
``create_app`` so importing the app in a test never starts syncing a firm's estate. The
app is the only process present in every supported deployment: the Hatchet worker does
not exist under ``orchestrator_provider != "hatchet"``, and a firm with no mounted folders
has no reason to deploy the watcher. Scheduling from the worker or the watcher would have
made "is my SharePoint being kept current" depend on which containers happen to be up.
Enqueuing is cheap and dispatch is the orchestrator's problem, so this holds under both
providers: with Hatchet the run is triggered on the worker, in-process it goes to the
sync thread pool, exactly as the sync button does.

**Due** means: not paused, not awaiting authorization, no sync already in flight, policy
mode continuous, and the source's last sync run started/finished at least one interval
ago. Deriving that from ``pipeline_runs`` rather than ``sources.last_sync_at`` matters —
a failing source never updates ``last_sync_at``, and would otherwise be re-enqueued on
every tick forever.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.connectors import scoping
from knowledge_index.connectors.registry import BY_NAME
from knowledge_index.db.models import PipelineRun as PipelineRunRecord, Source
from knowledge_index.sync.runs import ACTIVE_STATUSES, WORKFLOW, enqueue_sync

DEFAULT_INTERVAL_SECONDS = 300.0
# A floor on how often a source may be crawled, whatever the policy says: an interval of
# "1s" against a firm's DMS is a denial of service the operator did not intend.
MIN_INTERVAL_SECONDS = 5.0
# How long the loop may sleep between ticks. The upper bound keeps a newly added source
# with a short interval from waiting out a long sleep; the lower bound keeps the loop off
# the database when everything is due far in the future.
MIN_TICK_SECONDS = 5.0
MAX_TICK_SECONDS = 60.0
# Sources whose status means "there is nothing to sync", as opposed to "the last attempt
# failed" — an `error` source is retried on its interval, which is how it recovers once
# the operator fixes the credential at the provider.
NOT_SYNCABLE = ("paused", "pending_auth")


def _log(message: str) -> None:
    print(f"[ki schedule] {message}", file=sys.stderr, flush=True)


def interval_seconds(value: object, *, default: float = DEFAULT_INTERVAL_SECONDS) -> float:
    """Parse a sync-policy interval like ``"5m"`` / ``"30s"`` / ``"1h"`` (no regex)."""
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    unit_multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
    multiplier = unit_multipliers.get(text[-1])
    try:
        if multiplier is not None:
            return max(MIN_INTERVAL_SECONDS, float(text[:-1]) * multiplier)
        return max(MIN_INTERVAL_SECONDS, float(text))
    except ValueError:
        return default


def is_continuous(policy: dict | None) -> bool:
    """Whether this policy asks to be kept current without anyone clicking.

    An absent mode means continuous: that is what every connection the UI creates writes,
    and reading a missing value as "manual" would silently stop syncing sources that
    predate the field.
    """
    return (policy or {}).get("mode", "continuous") in ("continuous", None)


@dataclass(frozen=True)
class DueSource:
    source_id: str
    display_name: str
    kind: str
    interval: float
    # Seconds until this source is due; <= 0 means now.
    due_in: float


@dataclass
class TickReport:
    enqueued: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # When the earliest not-yet-due source becomes due, so the loop can sleep exactly
    # that long instead of guessing.
    next_due_in: float = MAX_TICK_SECONDS


def due_sources(session: Session, *, now: datetime | None = None) -> tuple[list[DueSource], float]:
    """Every continuous source that has waited out its interval, and when the next one is.

    Returns ``(due, seconds_until_next)``. Sources with a sync in flight are neither due
    nor counted towards the next wake-up: whatever is running will set the clock when it
    finishes.
    """
    moment = now or datetime.now(UTC)
    active = set(
        session.scalars(
            select(PipelineRunRecord.source_id).where(
                PipelineRunRecord.workflow == WORKFLOW,
                PipelineRunRecord.status.in_(ACTIVE_STATUSES),
            )
        ).all()
    )
    # The most recent sync attempt per source, whatever its outcome. finished_at when the
    # run is over, so a scan that takes longer than the interval spaces the next one from
    # its end rather than starting again the moment it lands.
    last_attempt = {
        source_id: stamp
        for source_id, stamp in session.execute(
            select(
                PipelineRunRecord.source_id,
                func.max(func.coalesce(PipelineRunRecord.finished_at, PipelineRunRecord.created_at)),
            )
            .where(PipelineRunRecord.workflow == WORKFLOW)
            .group_by(PipelineRunRecord.source_id)
        ).all()
        if source_id is not None
    }

    due: list[DueSource] = []
    next_due_in = MAX_TICK_SECONDS
    sources = session.scalars(
        select(Source)
        .where(Source.status.notin_(NOT_SYNCABLE))
        .order_by(Source.display_name, Source.id)
    ).all()
    for source in sources:
        if not is_continuous(source.sync_policy):
            continue
        if source.id in active:
            continue
        # OAuth completes before the operator can use the folder picker. Without this
        # guard a continuous source is immediately "due" and begins crawling the whole
        # SharePoint/Drive estate behind the open picker. Existing sources that already
        # synced predate the marker and continue normally; only a brand-new, never-synced
        # scopable source waits for an explicit folder/whole-source decision.
        spec = BY_NAME.get(source.kind)
        connector_config = (source.config or {}).get("connector") or {}
        if (
            spec is not None
            and spec.supports_scoping
            and source.last_sync_at is None
            and not last_attempt.get(source.id)
            and not scoping.describe(connector_config)["decided"]
        ):
            continue
        interval = interval_seconds((source.sync_policy or {}).get("interval"))
        # last_sync_at covers sources synced before this scheduler existed, or by a path
        # that left no run row; whichever is later wins, so an upgrade does not re-crawl
        # every estate at once.
        stamps = [
            _aware(value)
            for value in (last_attempt.get(source.id), source.last_sync_at)
            if value is not None
        ]
        if not stamps:
            due.append(DueSource(source.id, source.display_name, source.kind, interval, 0.0))
            continue
        elapsed = (moment - max(stamps)).total_seconds()
        due_in = interval - elapsed
        if due_in <= 0:
            due.append(DueSource(source.id, source.display_name, source.kind, interval, 0.0))
        else:
            next_due_in = min(next_due_in, due_in)
    return due, next_due_in


def tick(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> TickReport:
    """Enqueue one sync run for every source that is due. Safe to call concurrently.

    Overlap is not this module's to enforce: ``enqueue_sync`` reserves the run behind a
    per-source advisory lock and the ``uq_pipeline_runs_active_sync`` partial index, so a
    second scheduler, an operator's click and this tick cannot produce two crawls of one
    estate between them.
    """
    report = TickReport()
    with session_factory() as session:
        due, next_due_in = due_sources(session, now=now)
    report.next_due_in = next_due_in
    if not due:
        return report

    result = enqueue_sync(
        session_factory,
        config,
        source_ids={item.source_id for item in due},
        trigger="schedule",
    )
    names = {item.source_id: item.display_name for item in due}
    for run in result.runs:
        report.enqueued.append(run.source_id)
        _log(f"queued sync for {names.get(run.source_id, run.source_id)} (run {run.run_id})")
    for skipped in result.skipped:
        report.skipped.append((skipped.source_id, skipped.reason))
        _log(f"{names.get(skipped.source_id, skipped.source_id)} not queued: {skipped.reason}")
    return report


def run_scheduler_loop(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    *,
    stop_event: threading.Event | None = None,
    max_tick_seconds: float = MAX_TICK_SECONDS,
) -> None:
    """Tick until ``stop_event`` is set (or forever). Never raises out of the loop."""
    stop = stop_event or threading.Event()
    _log("scheduler started")
    while not stop.is_set():
        try:
            report = tick(session_factory, config_getter())
            sleep_for = min(max_tick_seconds, max(MIN_TICK_SECONDS, report.next_due_in))
        except Exception as exc:  # noqa: BLE001 - a failed tick must not stop scheduling
            # Losing the loop here would stop every source syncing, silently, which is the
            # defect this module exists to fix. Report and try again next tick.
            _log(f"tick failed: {type(exc).__name__}: {exc}")
            sleep_for = max_tick_seconds
        if stop.wait(sleep_for):
            break
    _log("scheduler stopped")


def start_background_scheduler(
    session_factory: sessionmaker[Session], config_getter: Callable[[], AppConfig]
) -> threading.Thread | None:
    """Run the scheduler beside a server process.

    ``KI_SYNC_SCHEDULE_SECONDS`` caps how long the loop sleeps between ticks (default 60);
    ``0`` leaves the scheduler out entirely, for a deployment that drives syncs from
    outside. That is deliberately the only way to turn scheduling off, because
    "continuous" in the admin UI has to mean something.
    """
    raw = os.environ.get("KI_SYNC_SCHEDULE_SECONDS", "").strip()
    if raw == "0":
        _log("scheduling disabled (KI_SYNC_SCHEDULE_SECONDS=0); nothing syncs on its own")
        return None
    max_tick = MAX_TICK_SECONDS
    if raw:
        try:
            max_tick = max(MIN_TICK_SECONDS, float(raw))
        except ValueError:
            _log(f"KI_SYNC_SCHEDULE_SECONDS is not a number ({raw!r}); using {max_tick:.0f}s")
    thread = threading.Thread(
        target=run_scheduler_loop,
        args=(session_factory, config_getter),
        kwargs={"max_tick_seconds": max_tick},
        name="ki-sync-scheduler",
        daemon=True,
    )
    thread.start()
    return thread


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
