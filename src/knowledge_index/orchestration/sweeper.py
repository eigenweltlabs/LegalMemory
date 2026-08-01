"""Resolve pipeline runs that nothing will ever advance.

A ``pipeline_runs`` row is a local mirror of orchestrated work, and a mirror can outlive
what it mirrors: a worker container is replaced mid-stage, a Hatchet task exhausts its
retries between two progress writes, a dispatch lands but the process recording its
workflow id dies. The row then sits at ``running`` forever. Two things go wrong, and the
second is the serious one:

* the admin dashboard reports work in flight that nothing is doing, and
* ``uq_pipeline_runs_active_sync`` refuses every later sync of that source, because a
  source with an unfinished sync run may not get a second one. A stranded run therefore
  stops a firm's estate from being indexed again, silently and indefinitely.

**No second progress mechanism.** Liveness is read from what the pipeline already
writes: ``pipeline_runs.updated_at`` (bumped by every progress refresh, both the sync
scan's observation counter and the insertion batch's per-stage aggregation) and
``processing_state.updated_at`` for the objects in an insertion batch. A run that is
genuinely working touches one of those constantly.

**The orchestrator is asked before anything is declared dead.** Silence alone is not
proof — a six-hour conversion is silent by design. Hatchet is authoritative about
whether the workflow run still exists, and for a sync run (one workflow run per sync)
about whether it finished. For an insertion batch it is not: ``provider_run_id`` there
is only the first of a bulk trigger, so its terminal status says nothing about the other
documents, and only "no such workflow run" is conclusive.

**Fail closed means leave it alone.** When the orchestrator cannot be asked — the local
provider has no liveness API, the engine is unreachable, no workflow id was ever
recorded — a run is abandoned only after a much longer silence, and is always failed
with the reason rather than deleted. ``scripts/reset-hatchet.sh`` handles the one case
this cannot reason about (the operator wiped the engine on purpose); this handles the
rest.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    PipelineRun as PipelineRunRecord,
    ProcessingState,
)

_log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
INSERTION_WORKFLOWS = ("insertion", "document-insertion", "access-refresh")

# How long a run may go without touching its own row, or any processing state in its
# batch, before the sweeper looks at it at all. Below this it is working, and the
# sweeper does not so much as query the orchestrator about it.
DEFAULT_SILENT_MINUTES = 15
# How long a silent run whose orchestrator cannot be asked has to stay silent before it
# is called abandoned. Deliberately longer than the longest single task the pipeline is
# configured to allow (a six-hour stage execution timeout, see orchestration/hatchet.py),
# so a legitimately slow stage is never mistaken for a dead one.
DEFAULT_ABANDONED_HOURS = 7

# (run provider, provider run id, workflow) -> (verdict, detail), where verdict is one of
# "alive", "gone", "terminal", "unknown".
OrchestratorProbe = Callable[[str, "str | None", str], tuple[str, str]]


@dataclass
class SweepReport:
    examined: int = 0
    left_running: int = 0
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    inconclusive: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return len(self.completed) + len(self.failed)

    def payload(self) -> dict:
        return {
            "examined": self.examined,
            "left_running": self.left_running,
            "completed": list(self.completed),
            "failed": list(self.failed),
            "inconclusive": list(self.inconclusive),
        }


def _positive_float_env(name: str, default: int) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        _log.warning("%s is not a number (%r); using %s", name, raw, default)
        return float(default)
    if value <= 0:
        _log.warning("%s must be positive (%r); using %s", name, raw, default)
        return float(default)
    return value


def silent_threshold() -> timedelta:
    return timedelta(minutes=_positive_float_env("KI_RUN_SILENT_MINUTES", DEFAULT_SILENT_MINUTES))


def abandoned_threshold() -> timedelta:
    return timedelta(hours=_positive_float_env("KI_RUN_ABANDONED_HOURS", DEFAULT_ABANDONED_HOURS))


def sweep_stranded_runs(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    now: datetime | None = None,
    orchestrator_status: OrchestratorProbe | None = None,
) -> SweepReport:
    """Resolve every active run that nothing is advancing. Safe to call concurrently.

    ``orchestrator_status`` is injectable so the decision logic can be tested without an
    engine; in production it defaults to querying whichever orchestrator is configured.
    """
    moment = now or datetime.now(UTC)
    silent_after = silent_threshold()
    abandoned_after = abandoned_threshold()
    probe = orchestrator_status or _default_probe(config)
    report = SweepReport()

    with session_factory() as session:
        candidates = session.scalars(
            select(PipelineRunRecord)
            .where(PipelineRunRecord.status.in_(ACTIVE_STATUSES))
            .order_by(PipelineRunRecord.created_at)
        ).all()
        # Snapshotted, then the session is released: probing the orchestrator can take
        # seconds per run and must not hold a connection open across it.
        pending = [
            (row.id, _last_activity(session, row), dict(row.counters or {}))
            for row in candidates
        ]

    for run_id, last_activity, counters in pending:
        report.examined += 1
        idle = moment - last_activity
        if idle < silent_after:
            report.left_running += 1
            continue
        outcome = _resolve_one(
            session_factory,
            config,
            run_id=run_id,
            counters=counters,
            idle=idle,
            abandoned_after=abandoned_after,
            probe=probe,
            now=moment,
        )
        if outcome == "completed":
            report.completed.append(run_id)
        elif outcome == "failed":
            report.failed.append(run_id)
        elif outcome == "alive":
            report.left_running += 1
        else:
            report.inconclusive.append(run_id)
    if report.resolved:
        _log.warning(
            "run sweeper resolved %d stranded run(s): %d completed, %d failed",
            report.resolved,
            len(report.completed),
            len(report.failed),
        )
    return report


# ------------------------------------------------------------------------- one run


def _resolve_one(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    run_id: str,
    counters: dict,
    idle: timedelta,
    abandoned_after: timedelta,
    probe: OrchestratorProbe,
    now: datetime,
) -> str:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None or record.status not in ACTIVE_STATUSES:
            # Another sweeper, or the run itself, got there first.
            return "alive"
        workflow = record.workflow
        provider = record.provider
        provider_run_id = record.provider_run_id
        last_step = record.current_step

    # A batch whose objects are all terminal is not stranded, only unwritten: the last
    # task died between finishing the work and recording it. Re-run the pipeline's own
    # aggregation rather than inventing a second opinion about what "done" means.
    if workflow in INSERTION_WORKFLOWS and counters.get("object_ids"):
        from knowledge_index.orchestration.hatchet import _refresh_batch_progress

        try:
            _refresh_batch_progress(session_factory, run_id, "complete")
        except Exception as exc:  # noqa: BLE001 - a reconcile failure must not strand the sweep
            _log.warning("run %s: progress reconcile failed: %s", run_id, exc)
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is not None and record.status == "completed":
                _log.info(
                    "run %s: every object had finished; the run was never marked complete",
                    run_id,
                )
                return "completed"

    verdict, detail = probe(provider, provider_run_id, workflow)
    if verdict == "alive":
        return "alive"
    if verdict == "gone":
        return _fail(
            session_factory,
            run_id,
            now=now,
            idle=idle,
            last_step=last_step,
            cause=(
                f"the orchestrator has no record of workflow run {provider_run_id}: "
                "the work this run was tracking no longer exists"
            ),
            detail=detail,
        )
    if verdict == "terminal":
        return _fail(
            session_factory,
            run_id,
            now=now,
            idle=idle,
            last_step=last_step,
            cause=(
                f"the orchestrator finished workflow run {provider_run_id} as {detail}, "
                f"but the run was never completed locally; it stopped at {last_step!r}"
            ),
            detail=detail,
        )

    # Nothing authoritative to go on. Only silence, so require a lot more of it.
    if idle < abandoned_after:
        _log.info(
            "run %s: silent for %s but the orchestrator could not confirm it is dead (%s)",
            run_id,
            _humanize(idle),
            detail,
        )
        return "inconclusive"
    return _fail(
        session_factory,
        run_id,
        now=now,
        idle=idle,
        last_step=last_step,
        cause=(
            f"no progress for {_humanize(idle)} and the orchestrator could not be asked "
            f"about it ({detail}); the worker that owned this run is gone"
        ),
        detail=detail,
    )


def _fail(
    session_factory: sessionmaker[Session],
    run_id: str,
    *,
    now: datetime,
    idle: timedelta,
    last_step: str | None,
    cause: str,
    detail: str,
) -> str:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None or record.status not in ACTIVE_STATUSES:
            return "alive"
        record.status = "failed"
        record.finished_at = record.finished_at or now
        # current_step is left as the pipeline last wrote it: where the run stopped is
        # the first thing an operator needs, and overwriting it with "swept" loses it.
        record.last_error = {
            "class": "StrandedRun",
            "message": cause,
            "idle_seconds": int(idle.total_seconds()),
            "last_step": last_step,
            "orchestrator": detail,
            "detected_by": "run-sweeper",
        }
        session.commit()
    _log.warning("run %s failed by the sweeper: %s", run_id, cause)
    return "failed"


def _last_activity(session: Session, record: PipelineRunRecord) -> datetime:
    """The most recent moment anything wrote progress for this run.

    The run row itself for every workflow; for an insertion batch also the processing
    states of the documents in it, because a batch of ten thousand files aggregates its
    row under an advisory lock and only one task at a time wins it.
    """
    stamps = [
        value
        for value in (record.updated_at, record.started_at, record.created_at)
        if value is not None
    ]
    object_ids = list((record.counters or {}).get("object_ids") or [])
    if record.workflow in INSERTION_WORKFLOWS and object_ids:
        newest = session.scalar(
            select(func.max(ProcessingState.updated_at)).where(
                ProcessingState.source_object_id.in_(object_ids)
            )
        )
        if newest is not None:
            stamps.append(newest)
    latest = max(stamps) if stamps else datetime.now(UTC)
    return latest if latest.tzinfo else latest.replace(tzinfo=UTC)


def _humanize(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours = minutes // 60
    return f"{hours} hour(s) {minutes % 60} minute(s)"


# ------------------------------------------------------------------ orchestrator probe


def _default_probe(config: AppConfig) -> OrchestratorProbe:
    provider = config.components.orchestrator_provider

    def probe(run_provider: str, provider_run_id: str | None, workflow: str) -> tuple[str, str]:
        if run_provider != "hatchet" or provider != "hatchet":
            return "unknown", f"the {run_provider} orchestrator has no liveness API"
        if not provider_run_id or ":" in provider_run_id:
            # "empty-batch:<id>" is a local placeholder, never a Hatchet workflow run.
            return "unknown", "no orchestrator workflow id was ever recorded for this run"
        return _hatchet_verdict(provider_run_id, workflow)

    return probe


def _hatchet_verdict(provider_run_id: str, workflow: str) -> tuple[str, str]:
    try:
        from hatchet_sdk import Hatchet

        status = str(Hatchet().runs.get_status(provider_run_id))
    except Exception as exc:  # noqa: BLE001 - any failure here means "we do not know"
        text = _compact(f"{type(exc).__name__}: {exc}")
        if _looks_like_not_found(exc):
            return "gone", text
        return "unknown", f"the orchestrator could not be reached ({text})"

    upper = status.rsplit(".", 1)[-1].strip("'\"").upper()
    if "QUEUED" in upper or "RUNNING" in upper:
        return "alive", upper
    if workflow in INSERTION_WORKFLOWS:
        # provider_run_id is the first workflow of a bulk trigger, so its completion says
        # nothing about the rest of the batch. Only its absence would be conclusive.
        return "unknown", f"first workflow of the batch reports {upper}; the batch may not be done"
    return "terminal", upper


def _compact(text: str, limit: int = 160) -> str:
    """One readable line. The SDK renders a 404 as a full HTTP header dump, and this
    string is stored on the run and shipped to every admin browser that polls it."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _looks_like_not_found(exc: BaseException) -> bool:
    """Whether the engine answered 'no such workflow run' rather than failing to answer.

    The distinction decides between failing a run now and waiting hours, so it is made on
    the HTTP status the SDK carries, not on message text.
    """
    for attribute in ("status", "status_code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and value == 404:
            return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404
