"""The nightly backup, driven by the same in-process loop pattern as sync scheduling.

A wall-clock schedule rather than an interval, because "every night at two" is what an
operator asks for and an interval drifts into the working day the first time a run is slow
or the appliance restarts. Due-ness is read from the run ledger, not from a timer held in
memory, so an appliance that was switched off over the weekend takes its backup when it
comes back rather than skipping to the next night — and a backup that failed does not
re-fire in a loop, because a failed attempt still counts as an attempt for that night.

The one piece of judgement here is what to do when the pipeline is busy at two in the
morning. Backing up mid-insertion produces a database that knows about documents whose
blobs the archive did not catch, so the loop waits. But waiting forever means an appliance
that is never idle is never backed up and nothing says so, which is the exact failure this
whole feature exists to prevent. So it waits up to ``defer_limit_minutes`` and then
enqueues anyway — unforced, so the run fails with the reason recorded on it and the
operator sees a red backup in the run list instead of a silence.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.backup.runs import (
    ACTIVE_STATUSES,
    WORKFLOW,
    BackupNotConfigured,
    count_unsettled,
    enqueue_backup,
)
from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun as PipelineRunRecord

# The loop only has to notice that a wall-clock minute has arrived, so it wakes often
# enough not to miss one and rarely enough to be invisible.
DEFAULT_TICK_SECONDS = 60.0
MIN_TICK_SECONDS = 5.0
TICK_SECONDS_ENV = "KI_BACKUP_SCHEDULE_SECONDS"


def _log(message: str) -> None:
    print(f"[ki backup-schedule] {message}", file=sys.stderr, flush=True)


@dataclass
class TickReport:
    enqueued: str | None = None
    skipped: str | None = None
    # The scheduled instant this tick was judging against, for tests and for the log.
    occurrence: datetime | None = None
    deferred: bool = False
    warnings: list[str] = field(default_factory=list)


def schedule_zone(config: AppConfig) -> ZoneInfo:
    """The timezone the schedule is written in, falling back to UTC rather than failing.

    Validation refuses an unknown name at save time, so reaching the fallback means the
    machine's timezone database changed under a config that was valid when it was written
    — worth a line in the log and not worth stopping the backups over.
    """
    name = config.backup.schedule.timezone or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - any resolution failure has the same answer
        _log(f"unknown schedule timezone {name!r}; falling back to UTC")
        return ZoneInfo("UTC")


def latest_occurrence(config: AppConfig, now: datetime) -> datetime:
    """The most recent moment the schedule fired, at or before ``now``.

    Resolved in the firm's own timezone, so "every night at two" stays at two across a
    daylight-saving change instead of walking an hour into the working day each spring.
    The two awkward days of the year are handled by construction rather than by special
    case: the local wall time is turned into an instant and compared as an instant, so a
    clock that skipped over 02:00 still yields exactly one occurrence for that date, and a
    clock that passed 02:00 twice yields the first of them.
    """
    schedule = config.backup.schedule
    zone = schedule_zone(config)
    local_now = now.astimezone(zone)
    today = _occurrence_on(local_now.date(), schedule, zone)
    if today <= now:
        return today
    return _occurrence_on(local_now.date() - timedelta(days=1), schedule, zone)


def _occurrence_on(day: date, schedule, zone: ZoneInfo) -> datetime:
    """The instant the schedule fires on one local date, as UTC."""
    local = datetime(day.year, day.month, day.day, schedule.hour, schedule.minute, tzinfo=zone)
    return local.astimezone(UTC)


def is_due(
    session: Session, config: AppConfig, *, now: datetime | None = None
) -> tuple[bool, datetime, str | None]:
    """Whether a backup is owed, the occurrence it is owed for, and why not if not.

    "Owed" means the schedule has fired since the last attempt — successful or not. Using
    the last *attempt* rather than the last success is what stops a persistently failing
    backup from re-enqueueing every minute of the night.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    occurrence = latest_occurrence(config, moment)
    if not config.backup.enabled:
        return False, occurrence, "backups are not enabled"
    if not config.backup.schedule.enabled:
        return False, occurrence, "the backup schedule is not enabled"

    active = session.scalar(
        select(PipelineRunRecord.id)
        .where(
            PipelineRunRecord.workflow == WORKFLOW,
            PipelineRunRecord.status.in_(ACTIVE_STATUSES),
        )
        .limit(1)
    )
    if active is not None:
        return False, occurrence, f"a backup is already in flight (run {active})"

    last_attempt = session.scalar(
        select(func.max(PipelineRunRecord.created_at)).where(
            PipelineRunRecord.workflow == WORKFLOW
        )
    )
    if last_attempt is not None and _aware(last_attempt) >= occurrence:
        return False, occurrence, "a backup has already been attempted for this occurrence"
    return True, occurrence, None


HEARTBEAT_NAME = "backup"
# How long a heartbeat may go unrefreshed before the appliance says nothing is watching.
# Generous against the default one-minute tick, because the answer has to survive a slow
# restart or a long-running backup holding the loop, and the question it answers is
# "has this been dead for hours", not "did it miss a beat".
HEARTBEAT_STALE_SECONDS = 15 * 60


def record_heartbeat(
    session: Session,
    moment: datetime,
    *,
    occurrence: datetime | None = None,
    reason: str | None = None,
) -> None:
    """Note that a scheduler loop is alive, and what it last decided.

    Written on every tick, due or not, and committed on its own so a heartbeat survives
    whatever the rest of the tick does. Failing to write one must never stop a backup
    being enqueued — the liveness record is there to explain a silence, not to cause one.
    """
    from knowledge_index.db.models import SchedulerHeartbeat

    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "occurrence": occurrence.isoformat() if occurrence else None,
        "last_decision": reason or "due",
    }
    try:
        record = session.get(SchedulerHeartbeat, HEARTBEAT_NAME)
        if record is None:
            session.add(
                SchedulerHeartbeat(name=HEARTBEAT_NAME, beat_at=moment, detail=payload)
            )
        else:
            record.beat_at = moment
            record.detail = payload
        session.commit()
    except Exception as exc:  # noqa: BLE001 - a missing heartbeat must not stop a backup
        session.rollback()
        _log(f"could not record the scheduler heartbeat: {type(exc).__name__}: {exc}")


def read_heartbeat(session_factory: sessionmaker[Session]) -> dict | None:
    """The backup scheduler's liveness, as the preflight report and the admin UI see it."""
    from knowledge_index.db.models import SchedulerHeartbeat

    try:
        with session_factory() as session:
            record = session.get(SchedulerHeartbeat, HEARTBEAT_NAME)
            if record is None:
                return None
            beat = _aware(record.beat_at)
            age = (datetime.now(UTC) - beat).total_seconds()
            return {
                "beat_at": beat.isoformat(),
                "age_seconds": round(age, 1),
                "alive": age <= HEARTBEAT_STALE_SECONDS,
                "detail": dict(record.detail or {}),
            }
    except Exception:  # noqa: BLE001 - an appliance mid-migration has no table yet
        return None


def tick(
    session_factory: sessionmaker[Session], config: AppConfig, *, now: datetime | None = None
) -> TickReport:
    """Enqueue tonight's backup if it is owed. Safe to call concurrently.

    Mutual exclusion is not this module's to enforce: ``enqueue_backup`` reserves behind an
    advisory lock, so a second scheduler, an operator's click and this tick cannot produce
    two backups between them.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    report = TickReport()
    with session_factory() as session:
        due, occurrence, reason = is_due(session, config, now=moment)
        report.occurrence = occurrence
        # Before deciding anything: record that something is watching the clock. A
        # schedule switched on in the admin UI over a deployment where no loop is running
        # is invisible from the configuration alone, and it is the same silent nothing
        # this whole feature exists to prevent.
        record_heartbeat(session, moment, occurrence=occurrence, reason=reason)
        if not due:
            report.skipped = reason
            return report
        defer = config.backup.schedule.defer_while_active
        unsettled = count_unsettled(session_factory) if defer else 0

    if unsettled:
        limit = timedelta(minutes=config.backup.schedule.defer_limit_minutes)
        if moment - occurrence < limit:
            report.deferred = True
            report.skipped = f"{unsettled} document(s) still mid-pipeline; waiting"
            return report
        # Past the deferral window. Enqueued unforced on purpose: it will fail, with the
        # reason on the run row, which is a visible red backup rather than another silent
        # night.
        report.warnings.append(
            f"the pipeline has been busy for {limit.total_seconds() / 60:.0f} minutes past "
            f"the scheduled backup; enqueueing anyway so the outcome is recorded"
        )
        _log(report.warnings[-1])

    try:
        enqueued = enqueue_backup(session_factory, config, trigger="schedule")
    except BackupNotConfigured as exc:
        report.skipped = str(exc)
        _log(f"not queued: {exc}")
        return report
    report.enqueued = enqueued.run_id
    _log(f"queued backup {enqueued.backup_id} (run {enqueued.run_id})")
    return report


def run_scheduler_loop(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    *,
    stop_event: threading.Event | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> None:
    """Tick until ``stop_event`` is set (or forever). Never raises out of the loop."""
    stop = stop_event or threading.Event()
    _log("backup scheduler started")
    while not stop.is_set():
        try:
            tick(session_factory, config_getter())
        except Exception as exc:  # noqa: BLE001 - a failed tick must not stop scheduling
            # Losing this loop would stop the appliance backing itself up, silently, which
            # is the one outcome this module exists to prevent. Report and try again.
            _log(f"tick failed: {type(exc).__name__}: {exc}")
        if stop.wait(tick_seconds):
            break
    _log("backup scheduler stopped")


def start_background_scheduler(
    session_factory: sessionmaker[Session], config_getter: Callable[[], AppConfig]
) -> threading.Thread | None:
    """Run the backup scheduler beside a server process.

    ``KI_BACKUP_SCHEDULE_SECONDS`` sets how often the loop looks at the clock (default 60);
    ``0`` leaves the thread out entirely, for a deployment that drives backups from cron or
    from another scheduler. Whether a backup is actually *taken* is still governed by
    ``backup.schedule.enabled`` in the admin UI — this variable only decides whether this
    process is one of the things watching.
    """
    raw = os.environ.get(TICK_SECONDS_ENV, "").strip()
    if raw == "0":
        _log(f"backup scheduling disabled ({TICK_SECONDS_ENV}=0); nothing backs up on its own")
        return None
    tick_seconds = DEFAULT_TICK_SECONDS
    if raw:
        try:
            tick_seconds = max(MIN_TICK_SECONDS, float(raw))
        except ValueError:
            _log(f"{TICK_SECONDS_ENV} is not a number ({raw!r}); using {tick_seconds:.0f}s")
    thread = threading.Thread(
        target=run_scheduler_loop,
        args=(session_factory, config_getter),
        kwargs={"tick_seconds": tick_seconds},
        name="ki-backup-scheduler",
        daemon=True,
    )
    thread.start()
    return thread


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
