"""Unit tests for the local-source watcher (the low-latency half of monitoring).

These exercise the pure helpers plus the sync/trigger orchestration against the real
test database; they do not start an OS watcher (that is covered end-to-end by the
docker stack) and they stub the pipeline trigger so no orchestrator is required.

The interval timetable lives in ``sync/scheduler.py`` and is tested in
``test_sync_scheduler.py``; the watcher only turns filesystem events into enqueued runs.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun, Source, SourceObject
from knowledge_index.sync import runs, watch
from knowledge_index.web.app import _connector_catalog


def _local_source(session: Session, root: Path, **kwargs) -> Source:
    source = Source(
        kind=kwargs.pop("kind", "local_fs"),
        provider=kwargs.pop("provider", "native"),
        display_name=kwargs.pop("display_name", root.name),
        config={"root": str(root)},
        sync_policy=kwargs.pop("sync_policy", {"mode": "continuous", "interval": "5m"}),
        **kwargs,
    )
    session.add(source)
    session.flush()
    return source


def test_sources_for_changes_maps_paths_to_roots(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    (root_a / "sub").mkdir(parents=True)
    root_b.mkdir()
    watched = [
        watch._WatchedSource("a", root_a.resolve()),
        watch._WatchedSource("b", root_b.resolve()),
    ]
    changed = {str(root_a / "sub" / "file.txt"), str(tmp_path / "unrelated.txt")}
    assert watch._sources_for_changes(watched, changed) == {"a"}


def test_load_watched_sources_selects_continuous_native_local(
    session: Session, factory: sessionmaker, tmp_path: Path
) -> None:
    watched_root = tmp_path / "watched"
    manual_root = tmp_path / "manual"
    paused_root = tmp_path / "paused"
    for directory in (watched_root, manual_root, paused_root):
        directory.mkdir()

    _local_source(session, watched_root)  # continuous native local_fs — included
    _local_source(session, manual_root, sync_policy={"mode": "manual"})  # excluded
    _local_source(session, paused_root, status="paused")  # excluded
    # An API-backed source is not a filesystem watch target.
    _local_source(session, watched_root, provider="native", kind="sharepoint_online")
    session.commit()

    watched = watch._load_watched_sources(factory)
    assert [source.root for source in watched] == [watched_root.resolve()]


def test_watcher_enqueues_runs_instead_of_scanning_on_its_own(
    session: Session, factory: sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    """The timer goes through the same enqueue path as the sync button.

    Timer-driven and operator-driven syncs must be the same thing: one run row each,
    visible in the ledger, and unable to overlap each other.
    """
    (tmp_path / "Mandate").mkdir()
    (tmp_path / "Mandate" / "Vertrag.txt").write_text("Inhalt", encoding="utf-8")
    source = _local_source(session, tmp_path)
    session.commit()

    submitted: list = []
    monkeypatch.setattr(runs, "_submit_local", submitted.append)
    config = AppConfig()
    config.components.orchestrator_provider = "local"

    watch._enqueue_sync(factory, lambda: config, {source.id})
    with factory() as verify:
        run_rows = verify.scalars(
            select(PipelineRun).where(PipelineRun.source_id == source.id)
        ).all()
    assert [(row.workflow, row.status) for row in run_rows] == [("source-sync", "queued")]
    # Recorded so a timer reconcile is distinguishable from a sync somebody asked for.
    assert run_rows[0].counters["trigger"] == "watch"
    assert len(submitted) == 1

    # The reserved run is still unfinished, so the next timer tick cannot start a second
    # crawl of the same folder.
    watch._enqueue_sync(factory, lambda: config, {source.id})
    with factory() as verify:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.source_id == source.id)
            )
            == 1
        )
    assert len(submitted) == 1

    # Running the reserved work does what the watcher used to do inline.
    submitted[0]()
    with factory() as verify:
        objects = verify.scalars(
            select(SourceObject).where(SourceObject.source_id == source.id)
        ).all()
    assert len(objects) == 1


def test_connector_catalog_offers_native_local_folder_first() -> None:
    catalog = _connector_catalog(AppConfig())
    assert catalog[0]["id"] == "local_fs"
    assert catalog[0]["provider"] == "native"
    assert catalog[0]["recommended"] is True
    assert catalog[0]["connectable"] is True
    plugin = next(entry for entry in catalog if entry["id"] == "plugin_drop")
    assert plugin["native"] is True and plugin["internal"] is True
    # API-backed connectors come from the connector registry, not from this list.
    assert all(entry["provider"] == "native" for entry in catalog)
