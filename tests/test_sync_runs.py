"""Source sync as an orchestrated pipeline: enqueue, overlap, handoff, reporting.

Everything here runs against the real test database and the real local-filesystem
connector. The only thing stubbed is the *dispatch* of reserved work, so a test can
observe the state the HTTP request left behind before any scanning happened — which is
the whole point of the change under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    PipelineRun,
    ProcessingState,
    Source,
    SourceObject,
)
from knowledge_index.sync import runs as sync_runs
from knowledge_index.taxonomies import (
    PIPELINE_STAGE_ORDER,
    PipelineStage,
    ProcessingStatus,
    stage_bucket,
)
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}


@pytest.fixture
def deferred_dispatch(monkeypatch) -> list:
    """Capture reserved sync work instead of running it.

    Lets a test assert on what the request returned *before* the scan starts, then run
    the work by hand. Without this the two are indistinguishable on a one-file corpus.
    """
    captured: list = []
    monkeypatch.setattr(sync_runs, "_submit_local", captured.append)
    return captured


def _local_app(
    factory: sessionmaker[Session], tmp_path: Path, **overrides
) -> tuple[TestClient, ConfigStore]:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.components.orchestrator_provider = "local"
    for key, value in overrides.items():
        setattr(config.pipeline, key, value)
    store.save(config)
    return TestClient(create_app(factory, store)), store


def _corpus(root: Path, count: int = 3) -> Path:
    (root / "Mandate").mkdir(parents=True)
    for index in range(count):
        (root / "Mandate" / f"Vertrag_{index}.txt").write_text("Inhalt", encoding="utf-8")
    return root


def _add_source(client: TestClient, root: Path, name: str = "Local matters") -> str:
    created = client.post(
        "/api/sources",
        json={
            "display_name": name,
            "kind": "local_fs",
            "provider": "native",
            "root": str(root),
            "sync_policy": {"mode": "manual"},
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _local_config(**overrides) -> AppConfig:
    config = AppConfig()
    config.components.orchestrator_provider = "local"
    for key, value in overrides.items():
        setattr(config.pipeline, key, value)
    return config


def test_sync_returns_202_with_run_ids_before_anything_is_scanned(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    client, _ = _local_app(factory, tmp_path)
    root = _corpus(tmp_path / "matters")
    with client:
        source_id = _add_source(client, root)
        response = client.post("/api/actions/sync", headers=ADMIN_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["skipped"] == []
    assert [run["source_id"] for run in body["runs"]] == [source_id]
    assert body["runs"][0]["display_name"] == "Local matters"
    run_id = body["runs"][0]["run_id"]

    # The proof that nothing was scanned inside the request: the run is still queued and
    # not one object exists, yet the response has already been produced.
    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record.workflow == "source-sync"
        assert record.status == "queued"
        assert record.progress == 0
        assert record.counters["observed"] == 0
        assert record.counters["mode"] is None
        assert record.counters["insertion_run_id"] is None
        assert record.counters["trigger"] == "api"
        assert session.scalar(select(func.count()).select_from(SourceObject)) == 0
    assert len(deferred_dispatch) == 1


def test_a_second_sync_while_one_is_in_flight_is_skipped_not_duplicated(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    client, _ = _local_app(factory, tmp_path)
    root = _corpus(tmp_path / "matters")
    with client:
        source_id = _add_source(client, root)
        first = client.post("/api/actions/sync", headers=ADMIN_HEADERS).json()
        second = client.post("/api/actions/sync", headers=ADMIN_HEADERS)

    assert second.status_code == 202
    body = second.json()
    assert body["runs"] == []
    assert len(body["skipped"]) == 1
    skipped = body["skipped"][0]
    assert skipped["source_id"] == source_id
    assert skipped["display_name"] == "Local matters"
    assert first["runs"][0]["run_id"] in skipped["reason"]

    # One reservation, one dispatch — not two crawls of the same estate.
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PipelineRun)) == 1
    assert len(deferred_dispatch) == 1


def test_the_database_refuses_a_second_active_sync_run_for_one_source(
    session: Session, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The guard is a database constraint, not only an application check."""
    from sqlalchemy.exc import IntegrityError

    source = Source(kind="local_fs", display_name="Local", config={"root": str(tmp_path)})
    session.add(source)
    session.commit()

    with factory() as first:
        first.add(
            PipelineRun(source_id=source.id, workflow="source-sync", status="running")
        )
        first.commit()
    with factory() as second:
        second.add(
            PipelineRun(source_id=source.id, workflow="source-sync", status="queued")
        )
        with pytest.raises(IntegrityError):
            second.commit()

    # A finished run is history and must not block the next sync.
    with factory() as third:
        third.add(
            PipelineRun(source_id=source.id, workflow="source-sync", status="completed")
        )
        third.commit()


def test_a_paused_source_is_skipped_with_a_reason(
    session: Session, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    session.add(
        Source(
            kind="local_fs",
            display_name="Paused folder",
            status="paused",
            config={"root": str(tmp_path)},
        )
    )
    session.commit()

    result = sync_runs.enqueue_sync(factory, _local_config())
    assert result.runs == []
    assert [item.reason for item in result.skipped] == ["source is paused"]
    with factory() as verify:
        assert verify.scalar(select(func.count()).select_from(PipelineRun)) == 0


def test_an_unknown_source_id_is_a_404(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = _local_app(factory, tmp_path)
    with client:
        response = client.post(
            "/api/actions/sync",
            json={"source_id": "00000000-0000-0000-0000-000000000000"},
            headers=ADMIN_HEADERS,
        )
    assert response.status_code == 404


def test_one_source_id_syncs_only_that_source(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    client, _ = _local_app(factory, tmp_path)
    first_root = _corpus(tmp_path / "first")
    second_root = _corpus(tmp_path / "second")
    with client:
        first_id = _add_source(client, first_root, name="First")
        _add_source(client, second_root, name="Second")
        body = client.post(
            "/api/actions/sync", json={"source_id": first_id}, headers=ADMIN_HEADERS
        ).json()

    assert [run["source_id"] for run in body["runs"]] == [first_id]
    assert body["skipped"] == []


def test_a_completed_sync_hands_off_to_insertion_and_records_the_run_id(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    client, _ = _local_app(factory, tmp_path)
    root = _corpus(tmp_path / "matters", count=2)
    with client:
        _add_source(client, root)
        run_id = client.post("/api/actions/sync", headers=ADMIN_HEADERS).json()["runs"][0][
            "run_id"
        ]
    deferred_dispatch[0]()

    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record.status == "completed"
        assert record.progress == 1
        assert record.counters["observed"] == 2
        assert record.counters["created"] == 2
        assert record.counters["mode"] == "full"
        insertion_run_id = record.counters["insertion_run_id"]
        assert insertion_run_id, "a sync that created documents must hand off to insertion"
        insertion = session.get(PipelineRun, insertion_run_id)
        assert insertion.workflow == "insertion"


def test_the_handoff_can_be_turned_off_so_nothing_is_converted_without_review(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    client, _ = _local_app(factory, tmp_path, auto_insert_after_sync=False)
    root = _corpus(tmp_path / "matters", count=2)
    with client:
        _add_source(client, root)
        run_id = client.post("/api/actions/sync", headers=ADMIN_HEADERS).json()["runs"][0][
            "run_id"
        ]
    deferred_dispatch[0]()

    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record.status == "completed"
        assert record.counters["created"] == 2
        assert record.counters["insertion_run_id"] is None
        # The documents are known; nothing was spent converting or embedding them.
        assert session.scalar(select(func.count()).select_from(SourceObject)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.workflow == "insertion")
            )
            == 0
        )


def test_a_sync_that_changed_nothing_completes_without_starting_insertion(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    client, _ = _local_app(factory, tmp_path, auto_insert_after_sync=False)
    root = _corpus(tmp_path / "matters", count=1)
    with client:
        _add_source(client, root)
        client.post("/api/actions/sync", headers=ADMIN_HEADERS)
        deferred_dispatch[0]()
        second_run_id = client.post("/api/actions/sync", headers=ADMIN_HEADERS).json()["runs"][
            0
        ]["run_id"]
    deferred_dispatch[1]()

    with factory() as session:
        record = session.get(PipelineRun, second_run_id)
        assert record.status == "completed"
        assert record.counters["unchanged"] == 1
        assert record.counters["created"] == 0
        assert record.counters["insertion_run_id"] is None


def test_access_only_change_hands_off_to_lightweight_index_refresh(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    with factory() as session:
        record = PipelineRun(
            workflow="source-sync",
            status="running",
            progress=0,
            current_step="handoff",
            counters={
                "created": 0,
                "changed": 0,
                "access_changed": 1,
                "restored": 0,
                "tombstoned": 0,
            },
        )
        session.add(record)
        session.commit()
        run_id = record.id

    launched: list[bool] = []

    def launch(_factory, _config):
        launched.append(True)
        return {"run_id": "access-index-run"}

    monkeypatch.setattr(
        "knowledge_index.orchestration.insertion.launch_insertion",
        launch,
    )
    insertion_run_id = sync_runs.run_handoff(factory, _local_config(), run_id)

    assert insertion_run_id == "access-index-run"
    assert launched == [True]
    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record is not None
        assert record.status == "completed"
        assert record.counters["insertion_run_id"] == "access-index-run"


def test_tombstone_only_sync_does_not_launch_global_insertion(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    with factory() as session:
        record = PipelineRun(
            workflow="source-sync",
            status="running",
            progress=0,
            current_step="handoff",
            counters={
                "created": 0,
                "changed": 0,
                "access_changed": 0,
                "restored": 0,
                "tombstoned": 3,
            },
        )
        session.add(record)
        session.commit()
        run_id = record.id

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("deleted objects must not relaunch unrelated insertion work")

    monkeypatch.setattr(
        "knowledge_index.orchestration.insertion.launch_insertion",
        unexpected_launch,
    )

    assert sync_runs.run_handoff(factory, _local_config(), run_id) is None
    with factory() as session:
        record = session.get(PipelineRun, run_id)
        assert record is not None
        assert record.status == "completed"
        assert record.current_step == "complete (deletions applied)"
        assert record.counters["insertion_run_id"] is None


def test_new_insertion_batch_excludes_objects_owned_by_an_active_batch(
    factory: sessionmaker[Session],
) -> None:
    from knowledge_index.orchestration.hatchet import _reserve_batch

    with factory() as session:
        source = Source(kind="local_fs", display_name="Files", config={"root": "/tmp"})
        session.add(source)
        session.flush()
        owned = SourceObject(
            source_id=source.id,
            external_id="owned",
            path="owned.docx",
            name="owned.docx",
        )
        new = SourceObject(
            source_id=source.id,
            external_id="new",
            path="new.docx",
            name="new.docx",
        )
        session.add_all([owned, new])
        session.flush()
        session.add_all(
            [
                ProcessingState(
                    source_object_id=owned.id,
                    stage="fetch",
                    status=ProcessingStatus.PENDING.value,
                ),
                ProcessingState(
                    source_object_id=new.id,
                    stage="fetch",
                    status=ProcessingStatus.PENDING.value,
                ),
            ]
        )
        active = PipelineRun(
            workflow="document-insertion",
            status="running",
            progress=0,
            counters={"object_ids": [owned.id], "objects_total": 1},
        )
        candidate = PipelineRun(
            workflow="insertion",
            status="queued",
            progress=0,
            counters={},
        )
        session.add_all([active, candidate])
        session.commit()
        candidate_id = candidate.id
        new_id = new.id

    rows, workflow = _reserve_batch(factory, candidate_id)

    assert workflow == "document-insertion"
    assert [row.id for row in rows] == [new_id]
    with factory() as session:
        candidate = session.get(PipelineRun, candidate_id)
        assert candidate is not None
        assert candidate.counters["object_ids"] == [new_id]


def test_a_broken_connector_fails_its_own_run_and_marks_only_its_own_source(
    factory: sessionmaker[Session], tmp_path: Path, deferred_dispatch: list
) -> None:
    """Per-source isolation: one unreadable estate must not stop the others."""
    client, _ = _local_app(factory, tmp_path, auto_insert_after_sync=False)
    healthy_root = _corpus(tmp_path / "healthy")
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    with client:
        healthy_id = _add_source(client, healthy_root, name="Healthy")
        broken_id = _add_source(client, missing_root, name="Broken")
        body = client.post("/api/actions/sync", headers=ADMIN_HEADERS).json()
    assert len(body["runs"]) == 2
    missing_root.rmdir()  # the folder disappears between reservation and scan
    for work in deferred_dispatch:
        work()

    by_source = {run["source_id"]: run["run_id"] for run in body["runs"]}
    with factory() as session:
        broken = session.get(PipelineRun, by_source[broken_id])
        assert broken.status == "failed"
        assert broken.last_error["class"]
        assert session.get(Source, broken_id).status == "error"

        healthy = session.get(PipelineRun, by_source[healthy_id])
        assert healthy.status == "completed"
        assert session.get(Source, healthy_id).status == "active"


def test_a_running_scan_publishes_its_observation_count(
    session: Session, factory: sessionmaker[Session], tmp_path: Path, monkeypatch
) -> None:
    """An operator watching a first sync sees a number move, not just "running"."""
    monkeypatch.setattr(sync_runs.SyncEngine, "PROGRESS_EVERY", 2)
    root = _corpus(tmp_path / "matters", count=5)
    session.add(Source(kind="local_fs", display_name="Local", config={"root": str(root)}))
    session.commit()

    seen: list[tuple[str, int]] = []
    original = sync_runs._progress_writer

    def spy(session_factory, run_id):
        publish = original(session_factory, run_id)

        def wrapped(result):
            publish(result)
            with session_factory() as verify:
                record = verify.get(PipelineRun, run_id)
                seen.append((record.current_step, record.counters["observed"]))

        return wrapped

    monkeypatch.setattr(sync_runs, "_progress_writer", spy)
    config = _local_config(auto_insert_after_sync=False)
    enqueued = sync_runs.enqueue_sync(factory, config)
    sync_runs.wait_for_local_runs(timeout=60)

    assert seen, "a scan of five files must report progress at least once"
    assert seen[0] == ("scan (2 observed)", 2)
    with factory() as verify:
        record = verify.get(PipelineRun, enqueued.runs[0].run_id)
        assert record.status == "completed"
        assert record.counters["observed"] == 5


def test_the_in_process_runner_makes_its_sync_observable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """No stub anywhere: the local orchestrator must reach a completed run on its own."""
    client, _ = _local_app(factory, tmp_path, auto_insert_after_sync=False)
    root = _corpus(tmp_path / "matters", count=2)
    with client:
        _add_source(client, root)
        response = client.post("/api/actions/sync", headers=ADMIN_HEADERS)
        assert response.status_code == 202
        run_id = response.json()["runs"][0]["run_id"]
        sync_runs.wait_for_local_runs(timeout=120)

        listed = client.get("/api/runs", headers=ADMIN_HEADERS).json()
        run = next(row for row in listed if row["id"] == run_id)
    assert run["workflow"] == "source-sync"
    assert run["status"] == "completed"
    assert run["progress"] == 1
    assert run["counters"]["created"] == 2
    assert run["started_at"] and run["finished_at"]
    assert run["error"] is None


def test_blocked_stages_report_waiting_rather_than_skipped(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The dashboard must not say the pipeline declined work it has not reached."""
    client, _ = _local_app(factory, tmp_path, auto_insert_after_sync=False)
    root = _corpus(tmp_path / "matters", count=4)
    with client:
        _add_source(client, root)
        client.post("/api/actions/sync", headers=ADMIN_HEADERS)
        sync_runs.wait_for_local_runs(timeout=120)
        pipeline = client.get("/api/status", headers=ADMIN_HEADERS).json()["pipeline"]

    assert pipeline[PipelineStage.FETCH.value] == {ProcessingStatus.PENDING.value: 4}
    for stage in PIPELINE_STAGE_ORDER:
        if stage == PipelineStage.FETCH:
            continue
        counts = pipeline[stage.value]
        assert counts == {"waiting": 4}, f"{stage.value} is waiting on fetch, not skipped"
        assert ProcessingStatus.SKIPPED.value not in counts


def test_stage_bucket_separates_waiting_and_disabled_from_a_real_skip() -> None:
    """Three things are stored as ``skipped`` and only one is the handler's judgement.

    ``disabled`` used to land in the handler's bucket, which is how a panel came to read
    "skipped by config: 45" while every stage was on — and how an operator learned that
    the stage toggle apparently does nothing."""
    waiting = {"reason": "waiting_for_previous_stage"}
    disabled = {"reason": "disabled_by_configuration"}
    assert stage_bucket("skipped", waiting) == "waiting"
    assert stage_bucket("skipped", disabled) == "disabled"
    assert stage_bucket("skipped", {"reason": "no_final_version"}) == "skipped"
    assert stage_bucket("skipped", None) == "skipped"
    assert stage_bucket("done", waiting) == "done"


def test_run_progress_counts_waiting_stages_as_unfinished(
    session: Session, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A ratio that counted blocked stages as settled would overstate completion."""
    from knowledge_index.orchestration.hatchet import _refresh_batch_progress, _register_batch

    source = Source(kind="local_fs", display_name="Local", config={"root": str(tmp_path)})
    session.add(source)
    session.flush()
    source_object = SourceObject(
        source_id=source.id, external_id="a", path="a.txt", name="a.txt"
    )
    session.add(source_object)
    session.flush()
    for index, stage in enumerate(PIPELINE_STAGE_ORDER):
        session.add(
            ProcessingState(
                source_object_id=source_object.id,
                stage=stage.value,
                status=(
                    ProcessingStatus.PENDING.value
                    if index == 0
                    else ProcessingStatus.SKIPPED.value
                ),
                last_error=None if index == 0 else {"reason": "waiting_for_previous_stage"},
            )
        )
    run = PipelineRun(workflow="insertion", status="queued")
    session.add(run)
    session.commit()

    _register_batch(factory, run.id, [source_object.id])
    _refresh_batch_progress(factory, run.id, PipelineStage.FETCH.value)

    with factory() as verify:
        record = verify.get(PipelineRun, run.id)
        assert record.status == "running"
        assert record.progress == 0  # nothing is settled, and nothing pretends to be
        stages = record.counters["stages"]
        assert stages[PipelineStage.FETCH.value] == {ProcessingStatus.PENDING.value: 1}
        assert stages[PipelineStage.CONVERT.value] == {"waiting": 1}


def test_access_only_batch_is_labeled_as_access_refresh(
    session: Session, factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from knowledge_index.orchestration.hatchet import _register_batch
    from knowledge_index.taxonomies import ACCESS_ONLY_REINDEX

    source = Source(kind="sharepoint_online", display_name="SharePoint", config={})
    session.add(source)
    session.flush()
    source_object = SourceObject(
        source_id=source.id,
        external_id="remote-file-1",
        path="Shared Documents/contract.docx",
        name="contract.docx",
    )
    session.add(source_object)
    session.flush()
    session.add(
        ProcessingState(
            source_object_id=source_object.id,
            stage=PipelineStage.INDEX.value,
            status=ProcessingStatus.PENDING.value,
            last_error={"reason": ACCESS_ONLY_REINDEX},
        )
    )
    run = PipelineRun(workflow="insertion", status="queued")
    session.add(run)
    session.commit()

    _register_batch(factory, run.id, [source_object.id])

    with factory() as verify:
        record = verify.get(PipelineRun, run.id)
        assert record is not None
        assert record.workflow == "access-refresh"
