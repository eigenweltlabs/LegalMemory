"""The timetable: every continuous source gets synced on its interval, whatever its kind.

Before this existed, ``sync_policy = {"mode": "continuous", "interval": "2m"}`` was read
only by the folder watcher and only for ``local_fs`` / ``plugin_drop``. A SharePoint
connection carrying the same policy displayed as "continuous" in the admin UI and was
never synced by anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun, Source
from knowledge_index.sync import runs, scheduler


def _source(session: Session, **kwargs) -> Source:
    source = Source(
        kind=kwargs.pop("kind", "sharepoint_online"),
        display_name=kwargs.pop("display_name", "SharePoint"),
        config=kwargs.pop("config", {"connector": {"scope_decided": True}}),
        sync_policy=kwargs.pop("sync_policy", {"mode": "continuous", "interval": "2m"}),
        **kwargs,
    )
    session.add(source)
    session.flush()
    return source


def _run(session: Session, source: Source, *, status: str, ago: timedelta | None) -> PipelineRun:
    record = PipelineRun(
        source_id=source.id,
        provider="local",
        workflow=runs.WORKFLOW,
        status=status,
        finished_at=None if ago is None else datetime.now(UTC) - ago,
    )
    session.add(record)
    session.flush()
    return record


def _local_config() -> AppConfig:
    config = AppConfig()
    config.components.orchestrator_provider = "local"
    return config


def test_interval_seconds_parses_units() -> None:
    assert scheduler.interval_seconds("30s") == 30.0
    assert scheduler.interval_seconds("5m") == 300.0
    assert scheduler.interval_seconds("1h") == 3600.0
    assert scheduler.interval_seconds("45") == 45.0
    # unparseable / empty falls back to the default; tiny values clamp to the floor
    assert scheduler.interval_seconds(None) == scheduler.DEFAULT_INTERVAL_SECONDS
    assert scheduler.interval_seconds("nonsense") == scheduler.DEFAULT_INTERVAL_SECONDS
    assert scheduler.interval_seconds("1s") == scheduler.MIN_INTERVAL_SECONDS


def test_a_remote_source_that_has_never_synced_is_due(session: Session) -> None:
    source = _source(session)
    session.commit()
    due, _next = scheduler.due_sources(session)
    assert [item.source_id for item in due] == [source.id]


def test_a_new_scopable_source_waits_for_the_folder_picker(session: Session) -> None:
    waiting = _source(
        session,
        kind="google_drive",
        display_name="Drive awaiting scope",
        config={},
    )
    whole_source = _source(
        session,
        kind="google_drive",
        display_name="Drive explicitly whole",
        config={"connector": {"roots": [], "scope_decided": True}},
    )
    session.commit()

    due, _next = scheduler.due_sources(session)
    assert [item.source_id for item in due] == [whole_source.id]
    assert waiting.id not in {item.source_id for item in due}


def test_a_remote_source_is_due_again_one_interval_after_its_last_run(
    session: Session,
) -> None:
    fresh = _source(session, display_name="Recent")
    stale = _source(session, display_name="Stale")
    _run(session, fresh, status="completed", ago=timedelta(seconds=20))
    _run(session, stale, status="completed", ago=timedelta(minutes=5))
    session.commit()

    due, next_due_in = scheduler.due_sources(session)
    assert [item.source_id for item in due] == [stale.id]
    # The one that is not due sets the next wake-up rather than a fixed poll interval.
    assert 0 < next_due_in <= 120


def test_a_failed_run_still_spaces_the_next_attempt(session: Session) -> None:
    """A source whose scans fail never updates last_sync_at.

    Reading due-ness off the source rather than off its runs would re-enqueue it on every
    tick forever, so the run ledger is what the clock is read from.
    """
    source = _source(session)
    _run(session, source, status="failed", ago=timedelta(seconds=10))
    session.commit()
    due, _next = scheduler.due_sources(session)
    assert due == []

    _run(session, source, status="failed", ago=timedelta(minutes=9))
    session.commit()
    # The most recent attempt decides, and that one is still inside the interval.
    due, _next = scheduler.due_sources(session)
    assert due == []


def test_a_source_already_syncing_is_never_due(session: Session) -> None:
    source = _source(session)
    _run(session, source, status="running", ago=None)
    session.commit()
    due, _next = scheduler.due_sources(session)
    assert due == []


def test_paused_pending_auth_and_manual_sources_are_never_due(session: Session) -> None:
    _source(session, display_name="Paused", status="paused")
    _source(session, display_name="Unauthorized", status="pending_auth")
    _source(session, display_name="Manual", sync_policy={"mode": "manual", "interval": "1m"})
    # An `error` source is retried: that is how it recovers once the credential is fixed.
    recovering = _source(session, display_name="Broken", status="error")
    session.commit()

    due, _next = scheduler.due_sources(session)
    assert [item.source_id for item in due] == [recovering.id]


def test_a_policy_without_a_mode_is_treated_as_continuous(session: Session) -> None:
    source = _source(session, sync_policy={"interval": "1m"})
    session.commit()
    assert [item.source_id for item in scheduler.due_sources(session)[0]] == [source.id]


def test_tick_enqueues_remote_sources_with_nobody_clicking(
    session: Session, factory: sessionmaker, tmp_path, monkeypatch
) -> None:
    remote = _source(session, display_name="SharePoint")
    folder = _source(
        session, kind="local_fs", display_name="Matters", config={"root": str(tmp_path)}
    )
    _source(session, display_name="Paused one", status="paused")
    session.commit()

    submitted: list = []
    monkeypatch.setattr(runs, "_submit_local", submitted.append)
    report = scheduler.tick(factory, _local_config())

    assert sorted(report.enqueued) == sorted([remote.id, folder.id])
    with factory() as verify:
        rows = verify.scalars(select(PipelineRun)).all()
    assert sorted(row.source_id for row in rows) == sorted([remote.id, folder.id])
    # Same trigger vocabulary as the button and the CLI, so the ledger says who asked.
    assert {row.counters["trigger"] for row in rows} == {"schedule"}
    assert len(submitted) == 2


def test_a_second_tick_cannot_start_a_second_crawl_of_one_source(
    session: Session, factory: sessionmaker, monkeypatch
) -> None:
    _source(session)
    session.commit()
    submitted: list = []
    monkeypatch.setattr(runs, "_submit_local", submitted.append)

    scheduler.tick(factory, _local_config())
    second = scheduler.tick(factory, _local_config())

    # The reserved run is unfinished, so the source is not even a candidate.
    assert second.enqueued == []
    with factory() as verify:
        assert len(verify.scalars(select(PipelineRun)).all()) == 1
    assert len(submitted) == 1
