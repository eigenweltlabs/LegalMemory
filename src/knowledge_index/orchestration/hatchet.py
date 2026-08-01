"""Hatchet workflow definitions for the durable pipelines.

**Document insertion.** Each source object gets its own workflow run with one visible
Hatchet task per pipeline stage. This removes corpus-wide stage barriers: as soon as one
file converts it can classify, relate, extract, and index while other files are still
converting. Database ``processing_state`` rows remain the idempotency boundary, so a
workflow migration or retry keeps every completed artifact and resumes at the first
unfinished stage.

**Source sync.** Each configured source gets its own workflow run with two tasks, scan
then handoff, over one ``pipeline_runs`` row. Same shape, same run ledger, same
dashboard as insertion — a scan is orchestrated work, not a long HTTP request.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    PipelineRun as PipelineRunRecord,
    ProcessingState,
    SourceObject,
)
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.sync import runs as sync_runs
from knowledge_index.taxonomies import (
    ACCESS_ONLY_REINDEX,
    PIPELINE_STAGE_ORDER,
    STAGE_BUCKET_DISABLED,
    PipelineStage,
    ProcessingStatus,
    stage_bucket,
)

ConfigGetter = Callable[[], AppConfig]
NONTERMINAL_STATUSES = {
    ProcessingStatus.PENDING.value,
    ProcessingStatus.RUNNING.value,
    ProcessingStatus.FAILED.value,
}


def _as_getter(config: AppConfig | ConfigGetter) -> ConfigGetter:
    return config if callable(config) else (lambda: config)


class InsertionInput(BaseModel):
    run_id: str
    source_object_id: str
    source_path: str


class SyncInput(BaseModel):
    run_id: str
    source_id: str


class BackupInput(BaseModel):
    run_id: str


class RestoreInput(BaseModel):
    run_id: str


@dataclass(frozen=True)
class HatchetRuntime:
    client: Hatchet
    workflow: Any
    access_workflow: Any
    sync_workflow: Any
    backup_workflow: Any
    restore_workflow: Any


def build_hatchet_runtime(
    session_factory: sessionmaker[Session], config: AppConfig | ConfigGetter
) -> HatchetRuntime:
    get_config = _as_getter(config)
    client = Hatchet()
    document_concurrency = _positive_int_env(
        "KI_HATCHET_DOCUMENT_CONCURRENCY", default=16, maximum=1024
    )
    relation_concurrency = _positive_int_env(
        "KI_RELATE_MODEL_CONCURRENCY", default=16, maximum=256
    )
    workflow = client.workflow(
        name="knowledge-index-document-insertion",
        description="One resumable insertion workflow per source document",
        input_validator=InsertionInput,
        version=f"5-c{document_concurrency}-r{relation_concurrency}",
        # Hatchet's GROUP_ROUND_ROBIN strategy advances this many document DAGs
        # through their child stages without scheduling the entire corpus root-first.
        # The stages are predominantly remote I/O, so the default is deliberately
        # higher than the CPU count and remains operator-tunable.
        concurrency=document_concurrency,
    )

    stages = [item.value for item in PIPELINE_STAGE_ORDER]
    parents: list[Any] = []

    def stage_handler(stage: str, *, final: bool):
        def execute(input: InsertionInput, ctx: Context) -> dict[str, Any]:
            del ctx
            result = PipelineRunner(session_factory, get_config()).run_stage_for_object(
                stage, input.source_object_id
            )
            status = _stage_status(session_factory, input.source_object_id, stage)
            if status in NONTERMINAL_STATUSES:
                _refresh_batch_progress(session_factory, input.run_id, stage)
                raise RuntimeError(
                    f"{input.source_path}: stage {stage} remains {status}; retrying task"
                )
            _refresh_batch_progress(
                session_factory, input.run_id, "complete" if final else stage
            )
            return {
                "source_object_id": input.source_object_id,
                "source_path": input.source_path,
                "stage": stage,
                "status": status,
                "counters": result.__dict__,
            }

        return execute

    for position, stage in enumerate(stages):
        task = workflow.task(
            name=stage,
            parents=parents,
            retries=6,
            backoff_factor=5.0,
            backoff_max_seconds=60,
            schedule_timeout=timedelta(hours=6),
            execution_timeout=timedelta(hours=6),
            concurrency=(
                relation_concurrency if stage == PipelineStage.RELATE.value else None
            ),
        )(stage_handler(stage, final=position == len(stages) - 1))
        parents = [task]

    return HatchetRuntime(
        client=client,
        workflow=workflow,
        access_workflow=_build_access_refresh_workflow(
            client,
            session_factory,
            get_config,
            concurrency=document_concurrency,
        ),
        sync_workflow=_build_sync_workflow(client, session_factory, get_config),
        backup_workflow=_build_backup_workflow(client, session_factory, get_config),
        restore_workflow=_build_restore_workflow(client, session_factory, get_config),
    )


def _build_access_refresh_workflow(
    client: Hatchet,
    session_factory: sessionmaker[Session],
    get_config: ConfigGetter,
    *,
    concurrency: int,
) -> Any:
    """Update indexed ACL projections without scheduling the document DAG."""
    workflow = client.workflow(
        name="knowledge-index-access-refresh",
        description="Refresh searchable permissions without reprocessing document content",
        input_validator=InsertionInput,
        version=f"1-c{concurrency}",
        concurrency=concurrency,
    )

    @workflow.task(
        name=PipelineStage.INDEX.value,
        retries=6,
        backoff_factor=5.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(hours=1),
        execution_timeout=timedelta(hours=1),
    )
    def refresh_access(input: InsertionInput, ctx: Context) -> dict[str, Any]:
        del ctx
        result = PipelineRunner(session_factory, get_config()).run_stage_for_object(
            PipelineStage.INDEX.value,
            input.source_object_id,
        )
        status = _stage_status(
            session_factory,
            input.source_object_id,
            PipelineStage.INDEX.value,
        )
        if status in NONTERMINAL_STATUSES:
            _refresh_batch_progress(
                session_factory,
                input.run_id,
                PipelineStage.INDEX.value,
            )
            raise RuntimeError(
                f"{input.source_path}: access refresh remains {status}; retrying task"
            )
        _refresh_batch_progress(session_factory, input.run_id, "complete")
        return {
            "source_object_id": input.source_object_id,
            "source_path": input.source_path,
            "stage": PipelineStage.INDEX.value,
            "status": status,
            "counters": result.__dict__,
        }

    return workflow


def _build_backup_workflow(
    client: Hatchet, session_factory: sessionmaker[Session], get_config: ConfigGetter
) -> Any:
    """One task, over one ``pipeline_runs`` row: capture the whole appliance.

    Not a DAG. The stores have to be captured, transferred and verified in one process
    holding one staging directory, and splitting that across tasks would mean either
    staging everything at once — the disk cost this design exists to avoid — or shipping
    gigabytes between tasks through Hatchet's payloads.
    """
    workflow = client.workflow(
        name="knowledge-index-backup",
        description="Capture every store to the configured destination, then verify it",
        input_validator=BackupInput,
        version="1",
        # Strictly one. A second concurrent backup would contend for the same staging
        # disk and the same OpenSearch snapshot repository; the run reservation already
        # refuses one, and this is the orchestrator-side backstop.
        concurrency=1,
    )

    @workflow.task(
        name="backup",
        # Deliberately no retries, for the same reason a scan has none: a backup fails on
        # an unmounted share, a missing key or a full disk, and none of those is fixed by
        # trying again immediately — while each attempt costs another full dump and
        # transfer. The run is left failed and visible, and the schedule tries again
        # tomorrow once somebody has fixed the cause.
        retries=0,
        schedule_timeout=timedelta(hours=6),
        # Long, because it has to cover a first backup of a large estate over a slow
        # mount. The per-component timeouts inside are the ones that catch a genuine hang.
        execution_timeout=timedelta(hours=24),
    )
    def backup(input: BackupInput, ctx: Context) -> dict[str, Any]:
        del ctx
        from knowledge_index.backup import runs as backup_runs

        return backup_runs.execute_backup_run(session_factory, get_config(), input.run_id)

    return workflow


def _build_restore_workflow(
    client: Hatchet, session_factory: sessionmaker[Session], get_config: ConfigGetter
) -> Any:
    """One task over one ``pipeline_runs`` row: stage a backup, verify it, apply what was asked.

    Separate from the backup workflow because the two must never run at once — one writes
    the estate out, the other writes it back in — and the reservation refuses either while
    the other is in flight. Concurrency 1 is the orchestrator-side half of that.
    """
    workflow = client.workflow(
        name="knowledge-index-restore",
        description="Stage and verify a backup, and apply the stores that were asked for",
        input_validator=RestoreInput,
        version="1",
        concurrency=1,
    )

    @workflow.task(
        name="restore",
        # No retries, for the reason a backup has none and more so: a restore that failed
        # halfway has already changed the appliance, and repeating it blindly changes it
        # again from a different starting point.
        retries=0,
        schedule_timeout=timedelta(hours=6),
        execution_timeout=timedelta(hours=24),
    )
    def restore(input: RestoreInput, ctx: Context) -> dict[str, Any]:
        del ctx
        from knowledge_index.backup import restore_runs

        return restore_runs.execute_restore_run(session_factory, get_config(), input.run_id)

    return workflow


def trigger_restore(
    session_factory: sessionmaker[Session], config: AppConfig | ConfigGetter, run_id: str
) -> str:
    """Hand one already-reserved restore run to Hatchet and return its workflow run id."""
    runtime = build_hatchet_runtime(session_factory, _as_getter(config))
    reference = runtime.restore_workflow.run(
        RestoreInput(run_id=run_id),
        wait_for_result=False,
        additional_metadata={"knowledge_index_run_id": run_id},
    )
    return reference.workflow_run_id


def trigger_backup(
    session_factory: sessionmaker[Session], config: AppConfig | ConfigGetter, run_id: str
) -> str:
    """Hand one already-reserved backup run to Hatchet and return its workflow run id."""
    runtime = build_hatchet_runtime(session_factory, _as_getter(config))
    reference = runtime.backup_workflow.run(
        BackupInput(run_id=run_id),
        wait_for_result=False,
        additional_metadata={"knowledge_index_run_id": run_id},
    )
    return reference.workflow_run_id


def _build_sync_workflow(
    client: Hatchet, session_factory: sessionmaker[Session], get_config: ConfigGetter
) -> Any:
    """One scan-then-handoff workflow per source, over one ``pipeline_runs`` row."""
    sync_concurrency = _positive_int_env("KI_HATCHET_SYNC_CONCURRENCY", default=4, maximum=64)
    workflow = client.workflow(
        name="knowledge-index-source-sync",
        description="One scan per configured source, then the handoff to insertion",
        input_validator=SyncInput,
        version=f"1-c{sync_concurrency}",
        # A scan is remote I/O against someone else's API, and a firm with forty
        # SharePoint sites should not open forty crawls at once against a tenant that
        # will start throttling. Operator-tunable for estates that can take more.
        concurrency=sync_concurrency,
    )

    @workflow.task(
        name="scan",
        # Deliberately no retries. A scan that failed almost always failed on a revoked
        # scope, an expired licence, or a tombstone guard — none of which a retry fixes,
        # and all of which would cost another full crawl to rediscover. The run is left
        # failed and visible; an operator retries it once the cause is fixed.
        retries=0,
        schedule_timeout=timedelta(hours=6),
        execution_timeout=timedelta(hours=12),
    )
    def scan(input: SyncInput, ctx: Context) -> dict[str, Any]:
        del ctx
        result = sync_runs.run_scan(session_factory, get_config(), input.run_id)
        return {"run_id": input.run_id, "source_id": input.source_id, **result.__dict__}

    @workflow.task(
        name="handoff",
        parents=[scan],
        # Unlike the scan, this is one cheap enqueue against local infrastructure, so a
        # transient failure is worth retrying rather than stranding a finished scan.
        retries=3,
        backoff_factor=5.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(hours=1),
        execution_timeout=timedelta(hours=1),
    )
    def handoff(input: SyncInput, ctx: Context) -> dict[str, Any]:
        del ctx
        insertion_run_id = sync_runs.run_handoff(session_factory, get_config(), input.run_id)
        return {"run_id": input.run_id, "insertion_run_id": insertion_run_id}

    return workflow


def trigger_source_sync(
    session_factory: sessionmaker[Session],
    config: AppConfig | ConfigGetter,
    run_id: str,
    source_id: str,
) -> str:
    """Hand one already-reserved sync run to Hatchet and return its workflow run id."""
    runtime = build_hatchet_runtime(session_factory, _as_getter(config))
    reference = runtime.sync_workflow.run(
        SyncInput(run_id=run_id, source_id=source_id),
        wait_for_result=False,
        additional_metadata={
            "knowledge_index_run_id": run_id,
            "source_id": source_id,
        },
    )
    return reference.workflow_run_id


def _positive_int_env(name: str, *, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}, got {value}")
    return value


def trigger_insertion(
    session_factory: sessionmaker[Session],
    config: AppConfig | ConfigGetter,
    run_id: str,
    *,
    limit_per_stage: int | None = None,
) -> str:
    """Bulk-trigger one idempotent Hatchet workflow for every unfinished object."""
    get_config = _as_getter(config)
    runner = PipelineRunner(session_factory, get_config())
    runner.requeue_outdated_stages()
    runner.requeue_newly_enabled_stages()
    runner.recover_stale_claims()

    rows, batch_workflow = _reserve_batch(
        session_factory,
        run_id,
        limit_per_stage=limit_per_stage,
    )
    if not rows:
        _refresh_batch_progress(session_factory, run_id, "complete")
        return f"empty-batch:{run_id}"

    runtime = build_hatchet_runtime(session_factory, get_config)
    workflow = (
        runtime.access_workflow if batch_workflow == "access-refresh" else runtime.workflow
    )
    items = [
        workflow.create_bulk_run_item(
            InsertionInput(
                run_id=run_id,
                source_object_id=row.id,
                source_path=row.path,
            ),
            key=f"{run_id}:{row.id}",
            additional_metadata={
                "knowledge_index_run_id": run_id,
                "source_object_id": row.id,
                "source_path": row.path,
            },
        )
        for row in rows
    ]
    references = workflow.run_many(items, wait_for_result=False)
    _mark_batch_triggered(session_factory, run_id)
    return references[0].workflow_run_id


def start_hatchet_worker(
    session_factory: sessionmaker[Session], config: AppConfig | ConfigGetter, *, slots: int = 4
) -> None:
    runtime = build_hatchet_runtime(session_factory, config)
    _start_run_sweeper(session_factory, _as_getter(config))
    # One worker for all workflows. Insertion tasks are short (one stage for one document)
    # so slots turn over quickly, and separate containers would add deployment machinery
    # without isolating a scarce local resource.
    runtime.client.worker(
        "knowledge-index-insertion-worker",
        slots=slots,
        workflows=[
            runtime.workflow,
            runtime.access_workflow,
            runtime.sync_workflow,
            runtime.backup_workflow,
            runtime.restore_workflow,
        ],
    ).start()


def _start_run_sweeper(session_factory: sessionmaker[Session], get_config: ConfigGetter) -> None:
    """Resolve stranded runs on a timer for as long as this worker lives.

    The admin UI sweeps too, but only while somebody is looking at it. A sync run left at
    ``running`` blocks every later sync of that source, so a firm whose operator does not
    open the dashboard for a week would silently stop indexing. Set
    ``KI_RUN_SWEEP_SECONDS=0`` to leave the timer out.
    """
    if os.environ.get("KI_RUN_SWEEP_SECONDS", "").strip() == "0":
        return
    interval = _positive_int_env("KI_RUN_SWEEP_SECONDS", default=300, maximum=86400)

    def loop() -> None:
        from knowledge_index.orchestration.sweeper import sweep_stranded_runs

        while True:
            time.sleep(interval)
            try:
                sweep_stranded_runs(session_factory, get_config())
            except Exception as exc:  # noqa: BLE001 - the sweeper must never kill the worker
                print(
                    f"[ki sweeper] sweep failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    threading.Thread(target=loop, name="ki-run-sweeper", daemon=True).start()


def _stage_status(
    session_factory: sessionmaker[Session], source_object_id: str, stage: str
) -> str | None:
    with session_factory() as session:
        return session.scalar(
            select(ProcessingState.status).where(
                ProcessingState.source_object_id == source_object_id,
                ProcessingState.stage == stage,
            )
        )


def _reserve_batch(
    session_factory: sessionmaker[Session],
    run_id: str,
    *,
    limit_per_stage: int | None = None,
) -> tuple[list[Any], str]:
    """Atomically assign only currently unowned objects to one insertion run.

    A source event can finish while an earlier document batch is still working. The
    handoff is global because it also drains durable retries, but an active batch already
    owns every object listed in its counters. Excluding those ids under one database lock
    lets a concurrent handoff pick up genuinely new work without starting a second
    Hatchet DAG for the same documents.
    """
    with session_factory() as session:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            lock_id = int.from_bytes(
                hashlib.blake2b(b"insertion-batch-assignment", digest_size=8).digest(),
                byteorder="big",
                signed=True,
            )
            session.execute(select(func.pg_advisory_xact_lock(lock_id)))

        assigned_ids: set[str] = set()
        active_batches = session.scalars(
            select(PipelineRunRecord).where(
                PipelineRunRecord.id != run_id,
                PipelineRunRecord.workflow.in_(
                    ("insertion", "document-insertion", "access-refresh")
                ),
                PipelineRunRecord.status.in_(("queued", "running")),
            )
        ).all()
        for batch in active_batches:
            assigned_ids.update(str(item) for item in (batch.counters or {}).get("object_ids", []))

        statement = (
            select(SourceObject.id, SourceObject.path)
            .join(ProcessingState, ProcessingState.source_object_id == SourceObject.id)
            .where(
                SourceObject.deleted_at.is_(None),
                ProcessingState.status.in_(NONTERMINAL_STATUSES),
            )
            .distinct()
            .order_by(SourceObject.path, SourceObject.id)
        )
        if assigned_ids:
            statement = statement.where(SourceObject.id.notin_(assigned_ids))
        rows = session.execute(statement).all()
        if limit_per_stage is not None:
            rows = rows[:limit_per_stage]
        object_ids = [row.id for row in rows]
        workflow = _record_batch(session, run_id, object_ids)
        session.commit()
        return rows, workflow


def _register_batch(
    session_factory: sessionmaker[Session], run_id: str, object_ids: list[str]
) -> str:
    """Record a preselected batch (kept as the unit-test and maintenance seam)."""
    with session_factory() as session:
        workflow = _record_batch(session, run_id, object_ids)
        session.commit()
        return workflow


def _record_batch(session: Session, run_id: str, object_ids: list[str]) -> str:
    record = session.get(PipelineRunRecord, run_id)
    if record is None:
        raise ValueError(f"pipeline run does not exist: {run_id}")
    nonterminal_states = (
        session.scalars(
            select(ProcessingState).where(
                ProcessingState.source_object_id.in_(object_ids),
                ProcessingState.status.in_(NONTERMINAL_STATUSES),
            )
        ).all()
        if object_ids
        else []
    )
    access_only = bool(nonterminal_states) and all(
        state.stage == PipelineStage.INDEX.value
        and (state.last_error or {}).get("reason") == ACCESS_ONLY_REINDEX
        for state in nonterminal_states
    )
    # The Hatchet workflow is shared because its durable stage tasks already skip
    # completed work. Keep the run ledger honest about what this particular batch
    # will do: an ACL reconciliation is not a document insertion.
    record.workflow = "access-refresh" if access_only else "document-insertion"
    record.status = "queued" if object_ids else "completed"
    record.current_step = "queued" if object_ids else "complete"
    record.progress = 0 if object_ids else 1
    record.counters = {
        "object_ids": object_ids,
        "objects_total": len(object_ids),
        "objects_completed": 0,
    }
    if not object_ids:
        record.finished_at = datetime.now(UTC)
    return record.workflow


def _mark_batch_triggered(session_factory: sessionmaker[Session], run_id: str) -> None:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            return
        record.status = "running"
        record.started_at = record.started_at or datetime.now(UTC)
        session.commit()


def _refresh_batch_progress(
    session_factory: sessionmaker[Session], run_id: str, current_stage: str
) -> None:
    with session_factory() as session:
        snapshot = session.get(PipelineRunRecord, run_id)
        if snapshot is None:
            return
        counters = dict(snapshot.counters or {})
        object_ids = list(counters.get("object_ids") or [])
        total = len(object_ids)

        # Dashboard refreshes are best-effort while work remains. Let exactly one task
        # aggregate state and make every other task return immediately instead of queuing
        # behind the shared pipeline-run row. The last document's final task takes the lock
        # normally so the run is guaranteed to reach completed rather than merely 99.9%.
        force_final_refresh = False
        if current_stage == "complete" and object_ids:
            force_final_refresh = (
                session.scalar(
                    select(ProcessingState.id)
                    .where(
                        ProcessingState.source_object_id.in_(object_ids),
                        ProcessingState.status.in_(NONTERMINAL_STATUSES),
                    )
                    .limit(1)
                )
                is None
            )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            lock_id = int.from_bytes(
                hashlib.blake2b(
                    f"pipeline-progress:{run_id}".encode(), digest_size=8
                ).digest(),
                byteorder="big",
                signed=True,
            )
            if force_final_refresh:
                session.execute(select(func.pg_advisory_xact_lock(lock_id)))
            elif not session.scalar(select(func.pg_try_advisory_xact_lock(lock_id))):
                session.rollback()
                return
            session.refresh(snapshot)
            counters = dict(snapshot.counters or {})

        if not total:
            record = snapshot
            record.status = "completed"
            record.progress = 1
            record.current_step = "complete"
            record.finished_at = record.finished_at or datetime.now(UTC)
            session.commit()
            return

        states = session.scalars(
            select(ProcessingState).where(ProcessingState.source_object_id.in_(object_ids))
        ).all()
        by_object: dict[str, list[ProcessingState]] = defaultdict(list)
        stage_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        completed_stage_units = 0
        for state in states:
            by_object[state.source_object_id].append(state)
            # The distinction the ratio below already makes has to reach the dashboard
            # too, or an operator reads "skipped" at every stage and concludes the
            # pipeline declined the corpus rather than that it has not got there yet.
            bucket = stage_bucket(state.status, state.last_error)
            stage_counts[state.stage][bucket] += 1
            if state.status in {ProcessingStatus.DONE.value, ProcessingStatus.QUARANTINED.value}:
                completed_stage_units += 1
            # A stage the handler declined and a stage that is switched off are both
            # settled — nothing will move either again. Only `waiting` is outstanding.
            elif bucket in {ProcessingStatus.SKIPPED.value, STAGE_BUCKET_DISABLED}:
                completed_stage_units += 1
        completed_objects = sum(
            1
            for object_id in object_ids
            if by_object.get(object_id)
            and not any(state.status in NONTERMINAL_STATUSES for state in by_object[object_id])
        )
        incomplete = total - completed_objects
        counters.update(
            {
                "objects_total": total,
                "objects_completed": completed_objects,
                "stages": {
                    stage: dict(sorted(counts.items()))
                    for stage, counts in sorted(stage_counts.items())
                },
            }
        )

        record = snapshot
        if record.status == "completed" and incomplete:
            session.commit()
            return
        record.counters = counters
        record.started_at = record.started_at or datetime.now(UTC)
        if incomplete == 0:
            record.status = "completed"
            record.progress = 1
            record.current_step = "complete"
            record.finished_at = record.finished_at or datetime.now(UTC)
        else:
            record.status = "running"
            denominator = max(1, total * len(PIPELINE_STAGE_ORDER))
            record.progress = min(0.999, completed_stage_units / denominator)
            record.current_step = f"{current_stage} ({completed_objects}/{total} files)"
        session.commit()
