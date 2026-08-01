"""Runs that nothing will ever advance have to stop being reported as in flight.

The observed failure: a ``pipeline_runs`` row reading ``complete (0/1 files)`` still at
``running`` hours after its worker had gone. Nothing advanced it and nothing ever would,
the dashboard reported work in progress, and — worse — a stranded *sync* run makes
``uq_pipeline_runs_active_sync`` refuse every later sync of that source.

The line these tests defend is that a run is only ever resolved on evidence: silence
plus either the orchestrator saying the work is gone, or silence long enough that no
configured task could still be running. A busy run is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    PipelineRun,
    ProcessingState,
    Source,
    SourceObject,
)
from knowledge_index.orchestration import sweeper
from knowledge_index.taxonomies import PIPELINE_STAGE_ORDER, ProcessingStatus


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.components.orchestrator_provider = "hatchet"
    return config


def _probe(verdict: str, detail: str = "test"):
    def answer(provider: str, provider_run_id: str | None, workflow: str) -> tuple[str, str]:
        del provider, provider_run_id, workflow
        return verdict, detail

    return answer


def _run(
    factory: sessionmaker[Session],
    *,
    workflow: str = "source-sync",
    status: str = "running",
    age: timedelta = timedelta(hours=2),
    provider_run_id: str | None = "wf-1",
    counters: dict | None = None,
    source_id: str | None = None,
) -> str:
    """A run whose row was last written ``age`` ago.

    ``updated_at`` carries ``onupdate=now()``, so the age has to be forced with SQL
    rather than assigned — an ORM write would immediately stamp it back to now.
    """
    stamp = datetime.now(UTC) - age
    with factory() as session:
        record = PipelineRun(
            source_id=source_id,
            provider="hatchet",
            provider_run_id=provider_run_id,
            workflow=workflow,
            status=status,
            current_step="complete (0/1 files)",
            counters=counters or {},
            started_at=stamp,
        )
        session.add(record)
        session.commit()
        run_id = record.id
        session.execute(
            text("UPDATE pipeline_runs SET updated_at = :stamp, created_at = :stamp WHERE id = :id"),
            {"stamp": stamp, "id": run_id},
        )
        session.commit()
    return run_id


def _status(factory: sessionmaker[Session], run_id: str) -> PipelineRun:
    with factory() as session:
        return session.get(PipelineRun, run_id)


def test_a_run_the_orchestrator_no_longer_knows_about_is_failed(factory, config):
    run_id = _run(factory)
    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("gone", "404 no such workflow run")
    )

    assert report.failed == [run_id]
    record = _status(factory, run_id)
    assert record.status == "failed"
    assert record.finished_at is not None
    assert "no record of workflow run wf-1" in record.last_error["message"]
    # The step it stopped at is the first thing an operator needs; the sweep keeps it.
    assert record.current_step == "complete (0/1 files)"
    assert record.last_error["last_step"] == "complete (0/1 files)"
    assert record.last_error["detected_by"] == "run-sweeper"


def test_a_run_the_orchestrator_says_is_alive_is_left_alone(factory, config):
    run_id = _run(factory, age=timedelta(days=2))
    report = sweeper.sweep_stranded_runs(factory, config, orchestrator_status=_probe("alive", "RUNNING"))

    assert report.failed == [] and report.left_running == 1
    assert _status(factory, run_id).status == "running"


def test_a_run_that_wrote_progress_recently_is_never_even_probed(factory, config):
    """A busy run must not depend on the orchestrator being reachable to survive."""
    run_id = _run(factory, age=timedelta(minutes=1))

    def explode(*args, **kwargs):
        raise AssertionError("an active run must not be probed")

    report = sweeper.sweep_stranded_runs(factory, config, orchestrator_status=explode)
    assert report.left_running == 1
    assert _status(factory, run_id).status == "running"


def test_silence_alone_is_not_enough_to_declare_a_run_dead(factory, config):
    """Six hours is a legal stage execution timeout; the sweeper waits it out."""
    run_id = _run(factory, age=timedelta(hours=2))
    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("unknown", "engine unreachable")
    )

    assert report.inconclusive == [run_id]
    assert _status(factory, run_id).status == "running"


def test_silence_past_the_abandonment_threshold_fails_the_run(factory, config):
    run_id = _run(factory, age=timedelta(hours=9))
    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("unknown", "engine unreachable")
    )

    assert report.failed == [run_id]
    record = _status(factory, run_id)
    assert record.status == "failed"
    assert "no progress for 9 hour(s)" in record.last_error["message"]
    assert "engine unreachable" in record.last_error["message"]


def test_a_sync_the_orchestrator_finished_without_us_is_failed_with_that_cause(factory, config):
    run_id = _run(factory, age=timedelta(hours=1))
    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("terminal", "FAILED")
    )

    assert report.failed == [run_id]
    message = _status(factory, run_id).last_error["message"]
    assert "finished workflow run wf-1 as FAILED" in message
    assert "never completed locally" in message


def test_failing_a_stranded_sync_lets_the_source_be_synced_again(factory, config):
    """The partial unique index is the real damage: one stranded run blocks them all."""
    from knowledge_index.sync import runs as sync_runs

    with factory() as session:
        source = Source(kind="local_fs", display_name="Mandate", config={"root": "/tmp/mandate"})
        session.add(source)
        session.commit()
        source_id = source.id

    stranded = _run(factory, source_id=source_id, age=timedelta(hours=3))
    blocked, reason = sync_runs._reserve_run(factory, source_id, "hatchet", "api")
    assert blocked is None and "already in flight" in reason

    sweeper.sweep_stranded_runs(factory, config, orchestrator_status=_probe("gone", "404"))
    assert _status(factory, stranded).status == "failed"

    reserved, reason = sync_runs._reserve_run(factory, source_id, "hatchet", "api")
    assert reserved is not None, reason


def _batch(factory: sessionmaker[Session], status: str) -> tuple[str, list[str]]:
    """One insertion run over one document, with that document's stages at ``status``."""
    with factory() as session:
        source = Source(kind="local_fs", display_name="Mandate", config={"root": "/tmp/mandate"})
        session.add(source)
        session.flush()
        obj = SourceObject(
            source_id=source.id,
            external_id="one",
            path="/Mandate/Vertrag.pdf",
            name="Vertrag.pdf",
        )
        session.add(obj)
        session.flush()
        for stage in PIPELINE_STAGE_ORDER:
            session.add(ProcessingState(source_object_id=obj.id, stage=stage.value, status=status))
        session.commit()
        return obj.id, [obj.id]


def test_a_finished_batch_that_was_never_written_completes_instead_of_failing(factory, config):
    """Calling done work 'failed' is as much a lie as calling dead work 'running'."""
    object_id, object_ids = _batch(factory, ProcessingStatus.DONE.value)
    del object_id
    run_id = _run(
        factory,
        workflow="document-insertion",
        age=timedelta(hours=9),
        counters={"object_ids": object_ids, "objects_total": 1, "objects_completed": 0},
    )
    with factory() as session:
        session.execute(
            text("UPDATE processing_state SET updated_at = now() - interval '9 hours'")
        )
        session.commit()

    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("unknown", "batch trigger, not conclusive")
    )
    assert report.completed == [run_id]
    record = _status(factory, run_id)
    assert record.status == "completed"
    assert record.progress == 1
    assert record.finished_at is not None


def test_a_batch_whose_documents_are_still_moving_is_left_alone(factory, config):
    """The run row can be quiet while the batch is not: one task at a time aggregates it."""
    _, object_ids = _batch(factory, ProcessingStatus.RUNNING.value)
    run_id = _run(
        factory,
        workflow="document-insertion",
        age=timedelta(hours=9),
        counters={"object_ids": object_ids, "objects_total": 1, "objects_completed": 0},
    )

    def explode(*args, **kwargs):
        raise AssertionError("a batch with live processing states must not be probed")

    report = sweeper.sweep_stranded_runs(factory, config, orchestrator_status=explode)
    assert report.left_running == 1
    assert _status(factory, run_id).status == "running"


def test_a_stuck_batch_is_failed_once_nothing_has_moved_for_long_enough(factory, config):
    _, object_ids = _batch(factory, ProcessingStatus.RUNNING.value)
    run_id = _run(
        factory,
        workflow="document-insertion",
        age=timedelta(hours=9),
        counters={"object_ids": object_ids, "objects_total": 1, "objects_completed": 0},
    )
    with factory() as session:
        session.execute(
            text("UPDATE processing_state SET updated_at = now() - interval '9 hours'")
        )
        session.commit()

    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("unknown", "first workflow of the batch")
    )
    assert report.failed == [run_id]
    assert "worker that owned this run is gone" in _status(factory, run_id).last_error["message"]


def test_a_run_with_no_orchestrator_id_is_never_asked_about_one(factory, config):
    """A dispatch that died before recording its workflow id has nothing to query."""
    run_id = _run(factory, provider_run_id=None, age=timedelta(hours=9))
    report = sweeper.sweep_stranded_runs(factory, config)  # the real probe, deliberately

    assert report.failed == [run_id]
    message = _status(factory, run_id).last_error["message"]
    assert "no orchestrator workflow id was ever recorded" in message


def test_an_empty_batch_placeholder_is_not_mistaken_for_a_workflow_id(factory, config):
    run_id = _run(factory, provider_run_id=f"empty-batch:{'0' * 8}", age=timedelta(hours=9))
    report = sweeper.sweep_stranded_runs(factory, config)
    assert report.failed == [run_id]


def test_the_threshold_is_operator_tunable(factory, config, monkeypatch):
    monkeypatch.setenv("KI_RUN_SILENT_MINUTES", "1")
    monkeypatch.setenv("KI_RUN_ABANDONED_HOURS", "0.5")
    run_id = _run(factory, age=timedelta(minutes=45))

    report = sweeper.sweep_stranded_runs(
        factory, config, orchestrator_status=_probe("unknown", "engine unreachable")
    )
    assert report.failed == [run_id]


def test_a_nonsense_threshold_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("KI_RUN_SILENT_MINUTES", "not-a-number")
    assert sweeper.silent_threshold() == timedelta(minutes=sweeper.DEFAULT_SILENT_MINUTES)
    monkeypatch.setenv("KI_RUN_ABANDONED_HOURS", "-3")
    assert sweeper.abandoned_threshold() == timedelta(hours=sweeper.DEFAULT_ABANDONED_HOURS)


def test_a_completed_run_is_not_reopened(factory, config):
    run_id = _run(factory, status="completed", age=timedelta(days=30))
    report = sweeper.sweep_stranded_runs(factory, config, orchestrator_status=_probe("gone", "404"))
    assert report.examined == 0
    assert _status(factory, run_id).status == "completed"
